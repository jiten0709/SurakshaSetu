"""GET /readyz (Step 25): each check (database, Redis, the domain tier's versions, OmniRoute's
/healthz, the pinned bundle as a turn loads it, the DUMMY gate) flips the answer on its own, and no
value (a DSN, URL, token or error text) reaches the body or the logs. No database, Redis or network:
a real Runtime over fakes, as in test_api.py."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from runtime_support import DOMAIN, VERSIONS, FakeGate, settings

from surakshasetu.api import app as app_module
from surakshasetu.api.app import READY_S, create_app
from surakshasetu.config import Settings
from surakshasetu.domain.client import DomainClient
from surakshasetu.gateway import Gateway
from surakshasetu.graph import side_query
from surakshasetu.graph.runtime import Runtime
from surakshasetu.graph.side_query import FaqError
from surakshasetu.rails.output import Verdict
from surakshasetu.store import conv as store

PG_DSN = "postgresql://app_rw:sentinel-pg-pw@db.invalid:5432/surakshasetu"
REDIS_URL = "redis://:sentinel-redis-pw@redis.invalid:6379/0"
DOMAIN_BEARER = "sentinel-domain-token"
GATEWAY_KEY = "sentinel-gateway-key"
FAQ_REASON = "sentinel-faq"
SENTINELS = (
    "sentinel-pg-pw",
    "sentinel-redis-pw",
    DOMAIN_BEARER,
    GATEWAY_KEY,
    FAQ_REASON,
    "postgresql://",
    "redis://",
    "http://",
)
CHECKS = ("database", "redis", "domain", "omniroute", "bundle", "dummy_gate")


class Stack:
    """What the fakes answer; a test breaks one dependency at a time."""

    def __init__(self) -> None:
        self.db_down = self.domain_down = self.gateway_down = False
        self.switches: set[tuple[str, str]] = set()
        self.timeouts: list[float | None] = []
        self.health_headers: list[httpx.Headers] = []

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[object]:
        self.timeouts.append(timeout)
        if self.db_down:
            raise psycopg.OperationalError(f"connection to {PG_DSN} failed")
        yield object()

    def domain(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/meta/versions"
        if self.domain_down:
            raise httpx.ConnectError(f"no route to {DOMAIN} with {DOMAIN_BEARER}")
        return httpx.Response(200, json=VERSIONS)

    def gateway(self, request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/healthz"
        self.health_headers.append(request.headers)
        if self.gateway_down:
            raise httpx.ConnectError(f"no route to {request.url} with {GATEWAY_KEY}")
        return httpx.Response(200, text="ok")


def config(**update: Any) -> Settings:
    return settings(
        pg_dsn_app=SecretStr(PG_DSN),
        redis_url=SecretStr(REDIS_URL),
        domain_token=SecretStr(DOMAIN_BEARER),
        gateway_api_key=SecretStr(GATEWAY_KEY),
        **update,
    )


@pytest.fixture
def stack(monkeypatch: pytest.MonkeyPatch) -> Stack:
    stack = Stack()
    monkeypatch.setattr(store, "active_kill_switches", lambda conn: stack.switches)
    return stack


def client(stack: Stack, cfg: Settings | None = None, gate: Any = None) -> TestClient:
    cfg = cfg or config()
    runtime = Runtime(
        cfg,
        pool=stack,  # type: ignore[arg-type]
        erasure=None,  # type: ignore[arg-type]
        keys=None,  # type: ignore[arg-type]
        gate=gate or FakeGate(),  # type: ignore[arg-type]
        domain=DomainClient(DOMAIN, DOMAIN_BEARER, transport=httpx.MockTransport(stack.domain)),
        gateway=Gateway(cfg, transport=httpx.MockTransport(stack.gateway)),
        pack=None,  # type: ignore[arg-type]
        graph=None,  # type: ignore[arg-type]
    )
    return TestClient(create_app(cfg, runtime=runtime))


def failing(response: httpx.Response) -> list[str]:
    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "SERVICE_UNAVAILABLE" and body["status"] == 503
    return list(body["failing"])


def test_ready_when_every_check_holds(stack: Stack) -> None:
    response = client(stack).get("/readyz")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"
    # The pool's own timeout bounds the wait (a cancelled getconn would leak a connection), and
    # OmniRoute's /healthz goes out without the app key.
    assert stack.timeouts == [READY_S]
    assert "authorization" not in stack.health_headers[0]


def break_database(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stack.db_down = True
    return {}


def break_redis(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return {"gate": FakeGate(down=True)}


def break_domain(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stack.domain_down = True
    return {}


def break_omniroute(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stack.gateway_down = True
    return {}


def unknown_bundle(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    return {"cfg": config(prompt_bundle="pb-2099.01.1")}


def kill_switched_bundle(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stack.switches = {("prompt_bundle", config().prompt_bundle)}
    return {}


def faq_refused(stack: Stack, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    def refuse(locale: str, env: str) -> None:
        raise FaqError("DUMMY_REFUSED")

    monkeypatch.setattr(side_query, "privacy_faq", refuse)
    return {}


@pytest.mark.parametrize(
    ("breaks", "name"),
    [
        (break_database, "database"),
        (break_redis, "redis"),
        (break_domain, "domain"),
        (break_omniroute, "omniroute"),
        (unknown_bundle, "bundle"),
        (kill_switched_bundle, "bundle"),
        (faq_refused, "dummy_gate"),
    ],
)
def test_each_check_flips_the_answer_alone(
    stack: Stack, monkeypatch: pytest.MonkeyPatch, breaks: Any, name: str
) -> None:
    response = client(stack, **breaks(stack, monkeypatch)).get("/readyz")

    assert failing(response) == [name]


def test_a_dummy_bundle_or_faq_in_pilot_is_not_ready(stack: Stack) -> None:
    pilot = config().model_copy(update={"env": "pilot"})

    assert failing(client(stack, pilot).get("/readyz")) == ["bundle", "dummy_gate"]


def test_the_dummy_gate_needs_rail_8_to_block_in_pilot(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The FAQ loads, but the rail only counts DUMMY text: a pilot would release it.
    pilot = config().model_copy(update={"env": "pilot"})
    monkeypatch.setattr(app_module, "pinned_bundle", lambda *args: None)
    monkeypatch.setattr(side_query, "privacy_faq", lambda locale, env: None)
    monkeypatch.setattr(
        app_module, "dummy", lambda text, env: Verdict("release", "RC-DUMMY", "count", 1.0)
    )

    assert failing(client(stack, pilot).get("/readyz")) == ["dummy_gate"]


def test_without_a_runtime_every_dependency_fails() -> None:
    response = TestClient(create_app(config())).get("/readyz")  # no lifespan: no runtime

    assert failing(response) == ["database", "redis", "domain", "omniroute"]


def test_redis_down_is_not_ready_but_still_live(stack: Stack) -> None:
    app = client(stack, gate=FakeGate(down=True))

    assert failing(app.get("/readyz")) == ["redis"]
    assert app.get("/healthz").status_code == 200


@pytest.mark.usefixtures("restore_logging")
def test_no_value_reaches_the_body_or_the_logs(
    stack: Stack,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Every check fails, each with an error whose text carries a secret or a URL.
    stack.db_down = stack.domain_down = stack.gateway_down = True

    class LeakyGate(FakeGate):
        async def ping(self) -> None:
            raise RedisConnectionError(f"Error connecting to {REDIS_URL}")

    def refuse(locale: str, env: str) -> None:
        raise FaqError(FAQ_REASON)

    monkeypatch.setattr(side_query, "privacy_faq", refuse)
    cfg = config(prompt_bundle="pb-2099.01.1", log_level="DEBUG", log_dir=tmp_path)

    response = client(stack, cfg, gate=LeakyGate()).get("/readyz")

    assert failing(response) == list(CHECKS)
    logs = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "not ready: " + ", ".join(CHECKS) in logs
    assert "readiness database failed: OperationalError" in logs
    for leaked in SENTINELS:
        assert leaked not in response.text
        assert leaked not in logs
