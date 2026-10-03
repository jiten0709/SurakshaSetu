"""The runtime against the migrated test database, committing for real as app_rw: one turn's conv
rows and audit events commit together before release (I8), a failure inside the commit leaves
neither, a crash between the commit and the checkpoint is repaired by hydrating from conv, and a
replay returns the same bytes. The model routes and the domain tier answer through
httpx.MockTransport and the Redis gate is in memory. Run with `make check-db`."""

import json
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
import pytest_asyncio
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from runtime_support import FakeGate, Models, domain, gateway, settings

from surakshasetu.api.app import create_app
from surakshasetu.audit.chain import SYSTEM_SESSION, verify_session
from surakshasetu.crypto.keys import LocalKeyService
from surakshasetu.graph.nodes import build_graph
from surakshasetu.graph.runtime import ProblemError, Runtime
from surakshasetu.logging import configure_logging
from surakshasetu.rails.output import load_pack
from surakshasetu.store import conv as store
from surakshasetu.uuid7 import uuid7

pytestmark = pytest.mark.db

SENTINEL_TEXT = "my PAN is ABCDE1234F and I live at 42 Sentinel Lane"


def app_dsn() -> str:
    dsn = os.environ.get("SS_TEST_PG_DSN_APP")
    if not dsn:
        pytest.fail("SS_TEST_PG_DSN_APP is not set; run `make up && make check-db`")
    return dsn


class NoCheckpoint(AsyncPostgresSaver):
    """The process dies after the commit: the checkpoint is never written."""

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return {"configurable": {**config["configurable"], "checkpoint_id": checkpoint["id"]}}

    async def aput_writes(self, *args: Any, **kwargs: Any) -> None:
        return None


@pytest.fixture
def created(admin_dsn: str) -> Iterator[list[UUID]]:
    """Session ids a test creates; their conv, audit and checkpoint rows go afterwards."""
    sessions: list[UUID] = []
    yield sessions
    threads = [str(s) for s in sessions]
    with psycopg.connect(admin_dsn) as conn:
        for table in ("conv.slot_value", "conv.turn", "conv.handoff", "conv.session"):
            conn.execute(f"DELETE FROM {table} WHERE session_id = ANY(%s)", (sessions,))  # noqa: S608
        conn.execute("DELETE FROM audit.audit_event WHERE session_id = ANY(%s)", (sessions,))
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(%s)", (threads,))  # noqa: S608
        conn.execute("DELETE FROM conv.kill_switch WHERE target LIKE 'test-route-%%'")


@pytest_asyncio.fixture
async def runtime(keys: LocalKeyService) -> AsyncIterator[Runtime]:
    dsn = app_dsn()
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
    config = settings()
    try:
        with ConnectionPool(dsn, min_size=1, max_size=4) as pool:
            async with gateway(Models(), config) as gw, domain() as dom:
                rt = Runtime(
                    config,
                    pool=pool,
                    keys=keys,
                    gate=FakeGate(),  # type: ignore[arg-type]
                    domain=dom,
                    gateway=gw,
                    pack=load_pack(config.output_lexicon),
                    graph=build_graph(AsyncPostgresSaver(saver_pool)),
                )
                rt.saver_pool = saver_pool  # type: ignore[attr-defined]
                yield rt
    finally:
        await saver_pool.close()


async def new_session(runtime: Runtime, created: list[UUID]) -> tuple[Any, str]:
    made = await runtime.create_session("web", "en-IN")
    created.append(UUID(made["session_id"]))
    row = await runtime.authenticate(UUID(made["session_id"]), made["session_token"])
    return row, made["session_token"]


def counts(runtime: Runtime, session_id: UUID) -> tuple[int, int]:
    with runtime.pool.connection() as conn:
        turns = conn.execute(
            "SELECT count(*) FROM conv.turn WHERE session_id = %s", (session_id,)
        ).fetchone()
        events = conn.execute(
            "SELECT count(*) FROM audit.audit_event WHERE session_id = %s", (session_id,)
        ).fetchone()
    assert turns is not None and events is not None
    return int(turns[0]), int(events[0])


async def checkpoint(runtime: Runtime, session_id: UUID) -> Any:
    snapshot = await runtime.graph.aget_state({"configurable": {"thread_id": str(session_id)}})
    return snapshot.values


@pytest.mark.asyncio
async def test_a_turn_commits_its_rows_and_events_together_and_the_chain_verifies(
    runtime: Runtime, created: list[UUID]
) -> None:
    row, _ = await new_session(runtime, created)

    released = json.loads(await runtime.run_turn(row, uuid7(), "hello", None))
    second = json.loads(await runtime.run_turn(row, uuid7(), "and again", None))

    assert released["state"] == "S0" and released["message"]["text"]
    assert second["turn_id"] != released["turn_id"]
    assert counts(runtime, row.session_id)[0] == 4
    with runtime.pool.connection() as conn:
        seqs = conn.execute(
            "SELECT seq, direction FROM conv.turn WHERE session_id = %s ORDER BY seq",
            (row.session_id,),
        ).fetchall()
        types = [
            r[0]
            for r in conn.execute(
                "SELECT event_type FROM audit.audit_event WHERE session_id = %s ORDER BY seq",
                (row.session_id,),
            )
        ]
        assert verify_session(conn, row.session_id).ok
        stored = store.get_session(conn, row.session_id)
    assert seqs == [(1, "in"), (2, "out"), (3, "in"), (4, "out")]
    assert types[0] == "TURN_INPUT" and "STATE_TRANSITION" in types
    assert types.count("RESPONSE_RELEASED") == 2 and types[-1] == "RESPONSE_RELEASED"
    values = await checkpoint(runtime, row.session_id)
    assert values["committed_seq"] == 4 and values["session"]["fsm_state"] == "S0"
    assert stored is not None and stored.counters == values["session"]["counters"]


@pytest.mark.asyncio
async def test_a_failure_inside_the_commit_leaves_no_conv_or_audit_rows(
    runtime: Runtime, created: list[UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    row, _ = await new_session(runtime, created)
    key = uuid7()

    def fail(*args: Any, **kwargs: Any) -> None:
        raise psycopg.OperationalError("disk full")

    with monkeypatch.context() as patch:
        patch.setattr(store, "update_session", fail)
        with pytest.raises(ProblemError) as excinfo:
            await runtime.run_turn(row, key, "hello", None)
    assert excinfo.value.status_code == 503

    assert counts(runtime, row.session_id) == (0, 0)  # TURN_INPUT and GUARD_VERDICTs too
    values = await checkpoint(runtime, row.session_id)
    assert values.get("session") is None and values.get("committed_seq", 0) == 0

    retried = json.loads(await runtime.run_turn(row, key, "hello", None))
    assert retried["state"] == "S0" and counts(runtime, row.session_id)[0] == 2


@pytest.mark.asyncio
async def test_a_crash_between_commit_and_checkpoint_is_repaired_by_hydration(
    runtime: Runtime, created: list[UUID], caplog: pytest.LogCaptureFixture
) -> None:
    row, _ = await new_session(runtime, created)
    await runtime.run_turn(row, uuid7(), "hello", None)
    graph = runtime.graph
    runtime.graph = build_graph(NoCheckpoint(runtime.saver_pool))  # type: ignore[attr-defined]
    await runtime.run_turn(row, uuid7(), "second", None)
    runtime.graph = graph
    assert (await checkpoint(runtime, row.session_id))["committed_seq"] == 2  # lags conv's 4

    with caplog.at_level("WARNING", logger="surakshasetu.graph.nodes"):
        third = json.loads(await runtime.run_turn(row, uuid7(), "third", None))

    assert "checkpoint lags conv (checkpoint seq 2, conv seq 4)" in caplog.text
    assert third["state"] == "S0"
    with runtime.pool.connection() as conn:
        seqs = [
            r[0]
            for r in conn.execute(
                "SELECT seq FROM conv.turn WHERE session_id = %s ORDER BY seq", (row.session_id,)
            )
        ]
        assert verify_session(conn, row.session_id).ok
    assert seqs == [1, 2, 3, 4, 5, 6]
    assert (await checkpoint(runtime, row.session_id))["committed_seq"] == 6


@pytest.mark.asyncio
async def test_a_retry_with_the_same_key_replays_the_same_bytes(
    runtime: Runtime, created: list[UUID]
) -> None:
    row, _ = await new_session(runtime, created)
    key = uuid7()

    first = await runtime.run_turn(row, key, "hello", None)
    runtime.gate.idem.clear()  # type: ignore[attr-defined]  # Redis lost the hint: conv answers
    again = await runtime.run_turn(row, key, "hello", None)

    assert again == first
    turns, events = counts(runtime, row.session_id)
    assert turns == 2
    with runtime.pool.connection() as conn:
        released = conn.execute(
            "SELECT count(*) FROM audit.audit_event"
            " WHERE session_id = %s AND event_type = 'RESPONSE_RELEASED'",
            (row.session_id,),
        ).fetchone()
    assert released == (1,)


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
async def test_no_customer_text_or_session_token_reaches_the_logs(
    runtime: Runtime, created: list[UUID], tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", tmp_path)
    row, token = await new_session(runtime, created)

    await runtime.run_turn(row, uuid7(), SENTINEL_TEXT, None)

    logged = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "transition S0 -> S0" in logged
    for leaked in ("ABCDE1234F", "Sentinel Lane", token, row.key_ref):
        assert leaked not in logged


@pytest.mark.asyncio
async def test_the_api_answers_a_wrong_token_exactly_like_an_unknown_session(
    runtime: Runtime, created: list[UUID]
) -> None:
    app = create_app(runtime.settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        opened = await client.post("/v1/sessions", json={"channel": "web", "locale": "en-IN"})
        assert opened.status_code == 201
        made = opened.json()
        session_id = made["session_id"]
        created.append(UUID(session_id))
        body = {"text": "hello"}
        key = {"Idempotency-Key": str(uuid7())}

        wrong = await client.post(
            f"/v1/sessions/{session_id}/turns",
            json=body,
            headers=key | {"Authorization": "Bearer guessed"},
        )
        unknown = await client.post(
            f"/v1/sessions/{uuid7()}/turns",
            json=body,
            headers=key | {"Authorization": f"Bearer {made['session_token']}"},
        )
        right = await client.post(
            f"/v1/sessions/{session_id}/turns",
            json=body,
            headers=key | {"Authorization": f"Bearer {made['session_token']}"},
        )

    assert made["events_url"] == f"/v1/sessions/{session_id}/events"
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.content == unknown.content
    assert right.status_code == 200 and right.json()["state"] == "S0"
    with runtime.pool.connection() as conn:
        stored = store.get_session(conn, UUID(session_id))
    assert stored is not None and stored.token_sha256 != made["session_token"].encode()
    assert stored.pins["rules"] == "2026.09.1" and stored.pins["consent_notice"] == "2026.09.1-en"


@pytest.mark.asyncio
@pytest.mark.usefixtures("system_chain_tail")
async def test_a_kill_switch_commits_its_row_and_a_system_chain_event(
    runtime: Runtime, created: list[UUID]
) -> None:
    target = f"test-route-{uuid7()}"

    await runtime.kill_switch("route", target, True, "ROUTE_DEGRADED", "ops")

    with runtime.pool.connection() as conn:
        assert ("route", target) in store.active_kill_switches(conn)
        event = conn.execute(
            "SELECT header FROM audit.audit_event WHERE session_id = %s"
            " AND event_type = 'KILL_SWITCH' AND header->>'target' = %s",
            (SYSTEM_SESSION, target),
        ).fetchone()
        assert verify_session(conn, SYSTEM_SESSION).ok
    assert event is not None and event[0]["reason_code"] == "ROUTE_DEGRADED"
