"""Inactivity timers (Step 22, TDD §3.9's CC3 and V3): which sessions a timer pauses (never one
before consent), the timer turn (CC3 -> PAUSE, no customer input, its own key), a session that moved
since it was selected, a busy session, and the settings' bounds."""

import dataclasses
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid5

import pytest
from pydantic import ValidationError
from runtime_support import settings
from test_runtime_nodes import Recorder, StoreCalls, row, rt, run, session, turn
from test_s0 import valid_record

from surakshasetu.config import Settings
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import nodes
from surakshasetu.graph.runtime import ProblemError, Runtime
from surakshasetu.graph.state import GraphState
from surakshasetu.jobs import timers
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
TIMEOUTS = {"S0": 600, "S1": 600, "QUOTE_ONLY": 600, "S2": 900, "S3": 1800}
CONSENT = UUID("0199a1b2-0000-7000-8000-0000000c0459")


def idle(
    state: str, minutes: int, *, consent: UUID | None = CONSENT, status: str = "active"
) -> Any:
    return row(
        fsm_state=state,
        consent_id=consent,
        status=status,
        last_activity_at=NOW - timedelta(minutes=minutes),
    )


def test_a_timer_pauses_only_post_consent_sessions_idle_past_their_states_timeout() -> None:
    rows = [
        idle("S1", 11),  # due
        idle("S1", 9),  # not yet
        idle("S2", 14),  # S2 waits 15 minutes
        idle("S3", 31),  # due
        idle("S0", 60, consent=None),  # before consent: passive (V3)
        idle("S0", 60),  # consent given, intent not chosen yet: due
        idle("S1", 60, status="paused"),  # already paused
        idle("EXIT", 60, status="ended"),
        idle("PAUSE", 60),  # no timeout for PAUSE
    ]

    due = timers.due(rows, NOW, TIMEOUTS)

    assert [(r.fsm_state, NOW - r.last_activity_at) for r in due] == [
        ("S1", timedelta(minutes=11)),
        ("S3", timedelta(minutes=31)),
        ("S0", timedelta(minutes=60)),
    ]


@pytest.mark.asyncio
async def test_a_timer_turn_reports_the_inactivity_and_cc3_pauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    writes = StoreCalls(monkeypatch)
    t = turn(text=None)
    t.action, t.timer = None, True
    t.next = session(
        fsm_state=FsmState.S1, consent=valid_record(), last_prompt_id="s1.ask:age_years"
    )
    t.row = row(fsm_state="S1")

    await run(t)
    await nodes.commit(GraphState(), rt(t))

    assert t.routed == "timer" and t.pipeline is None
    assert recorder.events[0]["payload"] == {"timer": "INACTIVITY"}
    assert t.transition is not None
    assert (t.transition.row_id, t.transition.reason_code, t.transition.to) == (
        "CC3",
        "INACTIVITY",
        FsmState.PAUSE,
    )
    assert t.released is not None and [i for i, _ in t.released.rendered.parts] == [  # type: ignore[union-attr]
        "template:paused"
    ]
    turn_in = next(kw for name, kw in writes.calls if name == "insert_turn")
    assert (turn_in["text"], turn_in["redacted"]) == (
        '{"timer":"INACTIVITY"}',
        "[timer:INACTIVITY]",
    )
    assert t.next is not None and t.next.stack[-1].state is FsmState.S1  # resumes there


@pytest.mark.asyncio
async def test_before_consent_a_timer_turn_moves_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """The job never selects such a session; if a timer turn ran anyway, CC3 would not match."""
    Recorder(monkeypatch)
    t = turn(text=None)
    t.action, t.timer = None, True
    t.next = session(fsm_state=FsmState.S0, last_prompt_id="s0.consent")

    await run(t)

    assert t.transition is not None and t.transition.to is FsmState.S0


class Fake:
    """A Runtime stand-in for run_once: its settings, a connection, and the timer turns asked."""

    def __init__(self, busy: set[UUID] | None = None) -> None:
        self.settings = settings(inactivity_timeouts=TIMEOUTS)
        self.busy, self.ran = busy or set(), []

    @asynccontextmanager
    async def connection(self) -> Any:
        yield None

    async def run_timer(self, row: SessionRow) -> bytes | None:
        if row.session_id in self.busy:
            raise ProblemError(409, "SESSION_BUSY")
        self.ran.append(row.session_id)
        return b"{}"


@pytest.mark.asyncio
async def test_one_look_runs_a_timer_turn_per_due_session_and_skips_a_busy_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b, c = (dataclasses.replace(idle("S1", 30), session_id=UUID(int=n)) for n in (1, 2, 3))
    asked: list[Any] = []

    def inactive(conn: Any, before: datetime, ids: Any = None) -> list[SessionRow]:
        asked.append((before, ids))
        return [a, b, c, idle("S1", 30, consent=None)]

    monkeypatch.setattr(store, "inactive_sessions", inactive)
    fake = Fake(busy={b.session_id})

    released = await timers.run_once(fake, NOW, [a.session_id])  # type: ignore[arg-type]

    assert asked == [(NOW - timedelta(seconds=600), [a.session_id])]
    assert fake.ran == [a.session_id, c.session_id] and len(released) == 2


@pytest.mark.asyncio
async def test_run_timer_refuses_before_consent_and_keys_the_turn_by_the_idle_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = Runtime.__new__(Runtime)
    keys: list[tuple[UUID, datetime | None]] = []

    async def fake_run(row: SessionRow, key: UUID, *a: Any, idle_since: Any = None) -> bytes:
        keys.append((key, idle_since))
        return b"{}"

    monkeypatch.setattr(runtime, "_run", fake_run)
    waiting = idle("S1", 30)

    assert await runtime.run_timer(idle("S0", 30, consent=None)) is None
    assert await runtime.run_timer(idle("S1", 30, status="paused")) is None
    assert await runtime.run_timer(waiting) == b"{}"
    assert await runtime.run_timer(waiting) == b"{}"
    want = uuid5(waiting.session_id, f"timer:{waiting.last_activity_at.isoformat()}")
    assert keys == [(want, waiting.last_activity_at)] * 2  # the same pause on a re-run


def test_timeouts_name_engaged_states_only_and_last_a_minute_at_least() -> None:
    with pytest.raises(ValidationError, match="no timer pauses"):
        Settings(_env_file=None, inactivity_timeouts={"PAUSE": 600})  # type: ignore[call-arg]
    with pytest.raises(ValidationError, match="at least 60"):
        Settings(_env_file=None, inactivity_timeouts={"S1": 30})  # type: ignore[call-arg]
