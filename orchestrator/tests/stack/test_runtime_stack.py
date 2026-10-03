"""The Conversation API against the running stack: the app in-process with its real lifespan
(Runtime.open), real valkey, domain-services, OmniRoute -> stubs, and the test database. Needs
`make up`, `make gateway-up` and `make check-stack`'s env (SS_TEST_PG_DSN_ADMIN, _KEYVAULT, _APP);
SS_TEST_REDIS_URL defaults to the compose valkey."""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
import pytest_asyncio
from pydantic import SecretStr
from redis.asyncio import Redis

from surakshasetu.api.app import create_app
from surakshasetu.config import Settings
from surakshasetu.graph.gate import RedisGate
from surakshasetu.uuid7 import uuid7

pytestmark = [pytest.mark.stack, pytest.mark.asyncio]

REDIS_URL = os.environ.get("SS_TEST_REDIS_URL", "redis://127.0.0.1:6379/0")


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is not set; run `make up && make gateway-up && make check-stack`")
    return value


def stack_settings() -> Settings:
    return Settings(
        _env_file=None,
        pg_dsn_app=SecretStr(env("SS_TEST_PG_DSN_APP")),
        pg_dsn_keyvault=SecretStr(env("SS_TEST_PG_DSN_KEYVAULT")),
        redis_url=SecretStr(REDIS_URL),
    )


@pytest.fixture
def created(admin_dsn: str) -> Iterator[list[UUID]]:
    sessions: list[UUID] = []
    yield sessions
    threads = [str(s) for s in sessions]
    with psycopg.connect(admin_dsn) as conn:
        for table in ("conv.turn", "conv.session"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ANY(%s)", (sessions,))  # noqa: S608
        conn.execute("DELETE FROM audit.audit_event WHERE session_id = ANY(%s)", (sessions,))
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(%s)", (threads,))  # noqa: S608


@pytest_asyncio.fixture
async def api(system_chain_tail: None) -> AsyncIterator[httpx.AsyncClient]:
    """Runtime.open activates the prompt bundle: its CONFIG_RELEASE goes afterwards."""
    app = create_app(stack_settings())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
            yield client


async def open_session(api: httpx.AsyncClient, created: list[UUID]) -> dict[str, str]:
    response = await api.post("/v1/sessions", json={"channel": "web", "locale": "en-IN"})
    assert response.status_code == 201, response.text
    made: dict[str, str] = response.json()
    created.append(UUID(made["session_id"]))
    return made


async def send(
    api: httpx.AsyncClient, made: dict[str, str], text: str, key: UUID | None = None
) -> httpx.Response:
    return await api.post(
        f"/v1/sessions/{made['session_id']}/turns",
        json={"text": text},
        headers={
            "Authorization": f"Bearer {made['session_token']}",
            "Idempotency-Key": str(key or uuid7()),
        },
    )


async def test_two_concurrent_turns_on_one_session_one_gets_409(
    api: httpx.AsyncClient, created: list[UUID]
) -> None:
    made = await open_session(api, created)

    first, second = await asyncio.gather(send(api, made, "hello"), send(api, made, "hi there"))

    assert sorted([first.status_code, second.status_code]) == [200, 409]
    busy = first if first.status_code == 409 else second
    assert busy.headers["content-type"] == "application/problem+json"
    assert busy.json()["code"] == "SESSION_BUSY"


async def test_a_retry_with_the_same_idempotency_key_is_byte_identical(
    api: httpx.AsyncClient, created: list[UUID]
) -> None:
    made = await open_session(api, created)
    key = uuid7()

    first = await send(api, made, "hello", key)
    retry = await send(api, made, "hello", key)
    other = await send(api, made, "hello")

    assert first.status_code == retry.status_code == other.status_code == 200
    assert retry.content == first.content
    assert other.json()["turn_id"] != first.json()["turn_id"]


async def test_the_session_audit_chain_verifies(
    api: httpx.AsyncClient, created: list[UUID]
) -> None:
    made = await open_session(api, created)
    assert (await send(api, made, "hello")).status_code == 200

    verified = await api.get(
        f"/internal/audit/sessions/{made['session_id']}/verify",
        headers={"Authorization": "Bearer surakshasetu-dev-compliance-key"},
    )

    assert verified.status_code == 200
    result: dict[str, Any] = verified.json()
    assert result["ok"] is True and result["checked"] >= 7  # TURN_INPUT .. RESPONSE_RELEASED

    listed = await api.get(
        f"/internal/audit/sessions/{made['session_id']}/events",
        headers={"Authorization": "Bearer surakshasetu-dev-compliance-key"},
    )
    events = listed.json()
    assert [e["seq"] for e in events] == list(range(1, result["checked"] + 1))
    assert events[0]["event_type"] == "TURN_INPUT"
    assert events[-1]["event_type"] == "RESPONSE_RELEASED"
    assert all("payload" not in e and "payload_enc" not in e for e in events)


async def test_the_redis_gate_on_valkey() -> None:
    redis = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        gate = RedisGate(redis, Settings(_env_file=None, rate_limits={60: 2}))
        session_id, subject_ref = uuid7(), uuid7()

        token = await gate.acquire(session_id)
        assert token is not None and await gate.acquire(session_id) is None
        await gate.release(session_id, "not-ours")  # someone else's token leaves the lock
        assert await gate.acquire(session_id) is None
        await gate.release(session_id, token)
        second = await gate.acquire(session_id)
        assert second is not None
        await gate.release(session_id, second)

        assert [await gate.over_limit(subject_ref) for _ in range(3)] == [False, False, True]

        await gate.set_idem(session_id, "ab" * 32)
        assert await gate.get_idem(session_id) == "ab" * 32

        events = gate.subscribe(session_id)
        receiver = asyncio.ensure_future(anext(events))
        await asyncio.sleep(0.2)  # let the subscription land
        await gate.publish(session_id, "turn.status", {"status": "analysing"})
        assert await asyncio.wait_for(receiver, 5) == ("turn.status", {"status": "analysing"})
        await events.aclose()  # type: ignore[attr-defined]
    finally:
        await redis.aclose()
