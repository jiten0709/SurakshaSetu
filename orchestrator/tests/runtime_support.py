"""Shared by the runtime tests (unit and db): the model routes and the domain tier behind
httpx.MockTransport (no network), an in-memory stand-in for the Redis gate, the dev pins, and (db
tests only) a real Runtime over the test database."""

import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import httpx
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError

from surakshasetu.config import Settings
from surakshasetu.crypto.keys import KeyService
from surakshasetu.domain.client import DomainClient
from surakshasetu.gateway import Gateway
from surakshasetu.graph.nodes import build_graph
from surakshasetu.graph.runtime import Runtime
from surakshasetu.graph.state import VersionPins
from surakshasetu.rails.output import load_pack

GATEWAY = "http://gateway.test/v1"
DOMAIN = "http://domain.test"
NOTICE = "2026.09.1-en"


def settings(**update: Any) -> Settings:
    return Settings(_env_file=None, gateway_base_url=GATEWAY, **update)


def pins() -> VersionPins:
    return VersionPins(
        prompt_bundle="pb-2026.10.1",
        rules="2026.09.1",
        corpus={},
        consent_notice=NOTICE,
        params="actuarial-dummy-2026.09.1",
        ranker="ranker-2026.09.1",
        registry="2026.09.1",
    )


class Models:
    """guard-input, nlu-extract and verify-claims. `intents`/`side_query` shape the analysis;
    `safety`/`injection` the guard's verdict; `down` names routes that fail."""

    def __init__(
        self,
        *,
        intents: tuple[str, ...] = (),
        side_query: str | None = None,
        safety: str = "safe",
        injection: float = 0.01,
        down: tuple[str, ...] = (),
    ) -> None:
        self.intents, self.side_query = intents, side_query
        self.safety, self.injection, self.down = safety, injection, down
        self.routes: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = json.loads(request.content)["model"]
        self.routes.append(route)
        if route in self.down:
            raise httpx.ConnectError("down")
        if route == "guard-input":
            answer: dict[str, Any] = {"injection_score": self.injection, "safety": self.safety}
        elif route == "nlu-extract":
            answer = {
                "intents": list(self.intents),
                "slots": [],
                "side_query": self.side_query,
                "language": "en",
            }
        elif route == "verify-claims":
            answer = {"verdict": "entailed"}
        else:
            raise AssertionError(f"unexpected route {route}")
        completion = {
            "model": f"stub-{route}",
            "choices": [{"message": {"content": json.dumps(answer)}}],
        }
        return httpx.Response(200, json=completion)


def gateway(models: Models, config: Settings | None = None) -> Gateway:
    return Gateway(config or settings(), httpx.MockTransport(models))


VERSIONS = {
    "rules_version": "2026.09.1",
    "params_version": "actuarial-dummy-2026.09.1",
    "ranker_version": "ranker-2026.09.1",
    "registry_version": "2026.09.1",
    "rating_version": "rating-dummy-2026.09.1",
    "active_rules_versions": ["2026.09.1"],
}


def domain_handler(request: httpx.Request) -> httpx.Response:
    """The two reference reads a session needs at creation."""
    if request.url.path == "/v1/meta/versions":
        return httpx.Response(200, json=VERSIONS)
    if request.url.path == "/v1/consent/notices/current":
        return httpx.Response(
            200,
            json={
                "notice_version": NOTICE,
                "language": "en-IN",
                "body": "DUMMY notice",
                "body_sha256": "ab" * 32,
                "is_dummy": True,
            },
        )
    raise AssertionError(f"unexpected domain call {request.method}")


def domain(handler: Callable[[httpx.Request], httpx.Response] = domain_handler) -> DomainClient:
    return DomainClient(DOMAIN, "t0ken", transport=httpx.MockTransport(handler))


def required_env(name: str) -> str:
    dsn = os.environ.get(name)
    if not dsn:
        pytest.fail(f"{name} is not set; run `make up && make check-db`")
    return dsn


@asynccontextmanager
async def db_runtime(
    keys: KeyService,
    *,
    models: "Models | None" = None,
    handler: Callable[[httpx.Request], httpx.Response] = domain_handler,
    **config: Any,
) -> AsyncIterator[Runtime]:
    """A real Runtime on the test database: app_rw and erasure_rw pools, the checkpointer on its
    own autocommit pool (as Runtime.open builds it), the model routes and the domain tier behind
    MockTransport, and the in-memory gate. `saver_pool` is exposed for a checkpointer swap."""
    dsn = required_env("SS_TEST_PG_DSN_APP")
    saver_pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": 0,
            "options": "-c search_path=langgraph",
        },
    )
    await saver_pool.open()
    cfg = settings(**config)
    try:
        with (
            ConnectionPool(dsn, min_size=1, max_size=4) as pool,
            ConnectionPool(required_env("SS_TEST_PG_DSN_ERASURE"), min_size=1) as erasure,
        ):
            async with gateway(models or Models(), cfg) as gw, domain(handler) as dom:
                rt = Runtime(
                    cfg,
                    pool=pool,
                    erasure=erasure,
                    keys=keys,
                    gate=FakeGate(),  # type: ignore[arg-type]
                    domain=dom,
                    gateway=gw,
                    pack=load_pack(cfg.output_lexicon),
                    graph=build_graph(AsyncPostgresSaver(saver_pool)),
                )
                rt.saver_pool = saver_pool  # type: ignore[attr-defined]
                yield rt
    finally:
        await saver_pool.close()


class FakeGate:
    """RedisGate in memory. `down` makes every call fail as a Redis outage does."""

    def __init__(self, *, limited: bool = False, down: bool = False) -> None:
        self.limited, self.down = limited, down
        self.locks: dict[UUID, str] = {}
        self.idem: dict[UUID, str] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.released: list[UUID] = []

    def _check(self) -> None:
        if self.down:
            raise RedisConnectionError("valkey down")

    async def acquire(self, session_id: UUID) -> str | None:
        self._check()
        if session_id in self.locks:
            return None
        self.locks[session_id] = "token"
        return "token"

    async def release(self, session_id: UUID, token: str) -> None:
        self._check()
        if self.locks.get(session_id) == token:
            del self.locks[session_id]
            self.released.append(session_id)

    async def over_limit(self, subject_ref: UUID) -> bool:
        self._check()
        return self.limited

    async def get_idem(self, turn_key: UUID) -> str | None:
        self._check()
        return self.idem.get(turn_key)

    async def set_idem(self, turn_key: UUID, rendered_sha256: str) -> None:
        self._check()
        self.idem[turn_key] = rendered_sha256

    async def publish(self, session_id: UUID, event: str, data: dict[str, Any]) -> None:
        self._check()
        self.published.append((event, data))

    async def subscribe(self, session_id: UUID) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """What was published so far, then the stream ends (Redis's never does)."""
        self._check()
        for event, data in list(self.published):
            yield event, data
