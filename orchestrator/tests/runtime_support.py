"""Shared by the runtime tests (unit and db): the model routes and the domain tier behind
httpx.MockTransport (no network), an in-memory stand-in for the Redis gate, and the dev pins."""

import json
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
from redis.exceptions import ConnectionError as RedisConnectionError

from surakshasetu.config import Settings
from surakshasetu.domain.client import DomainClient
from surakshasetu.gateway import Gateway
from surakshasetu.graph.state import VersionPins

GATEWAY = "http://gateway.test/v1"
DOMAIN = "http://domain.test"
NOTICE = "2026.09.1-en"


def settings(**update: Any) -> Settings:
    return Settings(_env_file=None, gateway_base_url=GATEWAY, **update)


def pins() -> VersionPins:
    return VersionPins(
        prompt_bundle="pb-2026.09.1",
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


def domain() -> DomainClient:
    return DomainClient(DOMAIN, "t0ken", transport=httpx.MockTransport(domain_handler))


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
