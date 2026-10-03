"""The Conversation API and the turn gate, with no database, Redis or network: a real Runtime over
an in-memory pool, gate and graph, and store.get_session replaced. Every error is problem+json."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from runtime_support import FakeGate, settings

from surakshasetu.api.app import BODY_LIMIT, create_app
from surakshasetu.audit.chain import VerifyResult
from surakshasetu.graph.runtime import ProblemError, Runtime
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

SID = UUID("0199a1b2-0000-7000-8000-00000000c0de")
OTHER = UUID("0199a1b2-0000-7000-8000-00000000dead")
KEY = "0199a1b2-0000-7000-8000-0000000000aa"
GOOD = "the-right-session-token"
SENTINEL = "my PAN is ABCDE1234F"
BODY = {"turn_id": "t", "state": "S0", "message": {"text": "Hi"}, "documents": []}


class FakeConn:
    def rollback(self) -> None:
        pass


class FakePool:
    def __init__(self) -> None:
        self.out = 0

    def getconn(self) -> FakeConn:
        self.out += 1
        return FakeConn()

    def putconn(self, conn: FakeConn) -> None:
        self.out -= 1


class FakeGraph:
    """ainvoke as the real graph ends: `response` set once committed, or an exception."""

    def __init__(self) -> None:
        self.calls = 0
        self.error: BaseException | None = None
        self.commit_first = False

    async def ainvoke(self, graph_input: Any, config: Any, *, context: Any, durability: str) -> Any:
        self.calls += 1
        assert durability == "exit" and config["configurable"]["thread_id"] == str(SID)
        if self.commit_first or self.error is None:
            context.response = BODY
        if self.error is not None:
            raise self.error
        return {}


def row(**update: Any) -> SessionRow:
    now = datetime.now(UTC)
    base = dict(
        session_id=SID,
        subject_ref=SID,
        key_ref="key-ref",
        channel="web",
        locale="en-IN",
        fsm_state="S0",
        frame_stack=[],
        pins={},
        status="active",
        created_at=now,
        last_activity_at=now,
        expires_at=now + timedelta(days=30),
        token_sha256=hashlib.sha256(GOOD.encode()).digest(),
        consent_id=None,
        counters={},
    )
    return SessionRow(**(base | update))  # type: ignore[arg-type]


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Runtime:
    sessions = {SID: row()}
    monkeypatch.setattr(store, "get_session", lambda conn, sid, lock=False: sessions.get(sid))
    rt = Runtime(
        settings(),
        pool=FakePool(),  # type: ignore[arg-type]
        keys=None,  # type: ignore[arg-type]
        gate=FakeGate(),  # type: ignore[arg-type]
        domain=None,  # type: ignore[arg-type]
        gateway=None,  # type: ignore[arg-type]
        pack=None,  # type: ignore[arg-type]
        graph=FakeGraph(),  # type: ignore[arg-type]
    )
    rt.sessions = sessions  # type: ignore[attr-defined]
    return rt


@pytest.fixture
def client(runtime: Runtime) -> TestClient:
    return TestClient(create_app(settings(), runtime=runtime), raise_server_exceptions=False)


def turn(
    client: TestClient,
    body: Any = None,
    *,
    bearer: str | None = GOOD,
    key: str = KEY,
    sid: UUID = SID,
) -> Any:
    headers = {"Idempotency-Key": key}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer}"
    return client.post(f"/v1/sessions/{sid}/turns", json=body or {"text": "hello"}, headers=headers)


def assert_problem(response: Any, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == code


# --- auth -----------------------------------------------------------------------------------------
def test_a_turn_returns_the_released_bytes(client: TestClient, runtime: Runtime) -> None:
    response = turn(client)

    assert response.status_code == 200
    assert (
        response.content == b'{"documents":[],"message":{"text":"Hi"},"state":"S0","turn_id":"t"}'
    )
    assert runtime.gate.released == [SID]  # type: ignore[attr-defined]


def test_a_wrong_token_and_an_unknown_session_get_the_same_401(client: TestClient) -> None:
    wrong = turn(client, bearer="guessed")
    unknown = turn(client, sid=OTHER)
    missing = turn(client, bearer=None)

    for response in (wrong, unknown, missing):
        assert_problem(response, 401, "UNAUTHORIZED")
    assert wrong.content == unknown.content == missing.content
    assert wrong.headers.keys() - {"x-request-id"} == unknown.headers.keys() - {"x-request-id"}


def test_an_expired_session_is_410(client: TestClient, runtime: Runtime) -> None:
    runtime.sessions[SID] = row(expires_at=datetime.now(UTC) - timedelta(seconds=1))  # type: ignore[attr-defined]

    assert_problem(turn(client), 410, "SESSION_EXPIRED")


def test_the_event_stream_relays_turn_events_to_the_session_holder(
    client: TestClient, runtime: Runtime
) -> None:
    runtime.gate.published.append(("turn.status", {"status": "analysing"}))  # type: ignore[attr-defined]
    runtime.gate.published.append(("turn.released", BODY))  # type: ignore[attr-defined]

    stream = client.get(f"/v1/sessions/{SID}/events", headers={"Authorization": f"Bearer {GOOD}"})

    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    events = [
        line.removeprefix("event: ")
        for line in stream.text.splitlines()
        if line.startswith("event:")
    ]
    data = [
        json.loads(line.removeprefix("data: "))
        for line in stream.text.splitlines()
        if line.startswith("data:")
    ]
    assert events == ["turn.status", "turn.released"] and data[1] == BODY
    assert_problem(client.get(f"/v1/sessions/{SID}/events"), 401, "UNAUTHORIZED")


# --- request validation ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        {"text": SENTINEL, "action": {"type": "QUICK_REPLY"}},
        {},
        {"text": SENTINEL, "extra": SENTINEL},
        {"action": {"type": "not a code", "payload": {"text": SENTINEL}}},
    ],
)
def test_an_invalid_turn_is_400_and_never_echoed(client: TestClient, body: Any) -> None:
    response = turn(client, body or {"nothing": 1})

    assert_problem(response, 400, "BAD_REQUEST")
    assert SENTINEL not in response.text and "nothing" not in response.text


@pytest.mark.parametrize("key", ["", "not-a-uuid"])
def test_the_idempotency_key_must_be_a_uuid(client: TestClient, key: str) -> None:
    assert_problem(turn(client, key=key), 400, "BAD_REQUEST")


def test_a_body_over_16_kib_is_413(client: TestClient, runtime: Runtime) -> None:
    response = turn(client, {"text": "x" * BODY_LIMIT})

    assert_problem(response, 413, "REQUEST_ENTITY_TOO_LARGE")
    assert runtime.graph.calls == 0  # type: ignore[attr-defined]


def test_an_unknown_route_is_a_problem(client: TestClient) -> None:
    assert_problem(client.get("/v1/nowhere"), 404, "NOT_FOUND")


# --- the turn gate --------------------------------------------------------------------------------
def test_a_held_lock_is_409_and_the_graph_never_runs(client: TestClient, runtime: Runtime) -> None:
    runtime.gate.locks[SID] = "someone-else"  # type: ignore[attr-defined]

    assert_problem(turn(client), 409, "SESSION_BUSY")
    assert runtime.graph.calls == 0  # type: ignore[attr-defined]
    assert runtime.gate.locks[SID] == "someone-else"  # type: ignore[attr-defined]


def test_the_rate_limit_is_429_and_frees_the_lock(client: TestClient, runtime: Runtime) -> None:
    runtime.gate.limited = True  # type: ignore[attr-defined]

    assert_problem(turn(client), 429, "RATE_LIMITED")
    assert runtime.graph.calls == 0 and runtime.gate.locks == {}  # type: ignore[attr-defined]


def test_redis_down_fails_closed_with_503(client: TestClient, runtime: Runtime) -> None:
    runtime.gate.down = True  # type: ignore[attr-defined]

    assert_problem(turn(client), 503, "SERVICE_UNAVAILABLE")
    assert runtime.graph.calls == 0  # type: ignore[attr-defined]


def test_a_failure_before_the_commit_is_503_and_releases_nothing(
    client: TestClient, runtime: Runtime
) -> None:
    runtime.graph.error = psycopg.OperationalError("audit insert failed")  # type: ignore[attr-defined]

    response = turn(client)

    assert_problem(response, 503, "SERVICE_UNAVAILABLE")
    assert "Hi" not in response.text and "audit insert" not in response.text
    assert runtime.gate.locks == {} and runtime.pool.out == 0  # type: ignore[attr-defined]


def test_a_checkpoint_failure_after_the_commit_still_releases(
    client: TestClient, runtime: Runtime
) -> None:
    runtime.graph.error = psycopg.OperationalError("checkpoint write failed")  # type: ignore[attr-defined]
    runtime.graph.commit_first = True  # type: ignore[attr-defined]

    response = turn(client)

    assert response.status_code == 200 and json.loads(response.content) == BODY


def test_a_row_lock_held_elsewhere_is_409(client: TestClient, runtime: Runtime) -> None:
    runtime.graph.error = psycopg.errors.LockNotAvailable()  # type: ignore[attr-defined]

    assert_problem(turn(client), 409, "SESSION_BUSY")


def test_a_bug_is_500_without_detail(client: TestClient, runtime: Runtime) -> None:
    runtime.graph.error = ValueError(SENTINEL)  # type: ignore[attr-defined]

    response = turn(client)

    assert_problem(response, 500, "INTERNAL_SERVER_ERROR")
    assert SENTINEL not in response.text


# --- internal endpoints ---------------------------------------------------------------------------
def test_internal_endpoints_take_their_own_role_key(
    client: TestClient, runtime: Runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def verify(session_id: UUID) -> VerifyResult:
        return VerifyResult(ok=True, checked=7)

    async def kill_switch(*args: Any) -> UUID:
        return OTHER

    async def audit_headers(session_id: UUID) -> list[dict[str, Any]]:
        return [{"seq": 1, "event_type": "TURN_INPUT", "header": {}}]

    monkeypatch.setattr(runtime, "audit_headers", audit_headers)
    monkeypatch.setattr(runtime, "verify", verify)
    monkeypatch.setattr(runtime, "kill_switch", kill_switch)
    ops = {"Authorization": "Bearer surakshasetu-dev-ops-key"}
    compliance = {"Authorization": "Bearer surakshasetu-dev-compliance-key"}
    url = f"/internal/audit/sessions/{SID}/verify"
    switch = {"kind": "product", "target": "999N001V02", "reason_code": "MIS_SELLING_RISK"}

    assert client.get(url, headers=compliance).json() == {
        "ok": True,
        "checked": 7,
        "first_bad_seq": None,
        "gap_at": None,
    }
    assert_problem(client.get(url, headers=ops), 403, "FORBIDDEN")
    listing = f"/internal/audit/sessions/{SID}/events"
    assert client.get(listing, headers=compliance).json()[0]["event_type"] == "TURN_INPUT"
    assert_problem(client.get(listing, headers=ops), 403, "FORBIDDEN")
    assert_problem(client.get(url), 401, "UNAUTHORIZED")
    assert_problem(
        client.get(url, headers={"Authorization": f"Bearer {GOOD}"}), 401, "UNAUTHORIZED"
    )

    created = client.post("/internal/kill-switches", json=switch, headers=ops)
    assert created.status_code == 201 and created.json()["id"] == str(OTHER)
    assert_problem(
        client.post("/internal/kill-switches", json=switch, headers=compliance), 403, "FORBIDDEN"
    )


@pytest.mark.asyncio
async def test_a_product_kill_switch_cannot_be_reversed(runtime: Runtime) -> None:
    with pytest.raises(ProblemError) as excinfo:
        await runtime.kill_switch("product", "999N001V02", False, "MISTAKE", "ops")
    assert excinfo.value.code == "KILL_SWITCH_IRREVERSIBLE"
