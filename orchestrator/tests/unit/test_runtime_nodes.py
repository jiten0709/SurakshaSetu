"""The graph's node functions with fakes: no database, no Redis, no network. Audit appends are
recorded, the model routes answer through httpx.MockTransport, and the store's SQL is replaced
where a node would reach it. The db and stack tests run the same nodes for real."""

import hashlib
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid5

import pytest
from langgraph.constants import END
from runtime_support import FakeGate, Models, domain, gateway, pins, settings

from surakshasetu.analysis.models import Intent, TurnAnalysis
from surakshasetu.analysis.pipeline import PipelineResult
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import EventType
from surakshasetu.compose.bundle import BundleError, load_bundle
from surakshasetu.domain.client import DomainError
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import nodes, states
from surakshasetu.graph.nodes import SlotRow, Turn, turn_router
from surakshasetu.graph.state import Frame, GraphState, SessionState
from surakshasetu.logging import configure_logging
from surakshasetu.rails.output import load_pack
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

SID = UUID("0199a1b2-0000-7000-8000-00000000c0de")
SUBJECT = UUID("0199a1b2-0000-7000-8000-00000000beef")
KEY = UUID("0199a1b2-0000-7000-8000-0000000000aa")
BUNDLE = load_bundle("pb-2026.10.8", env="dev")
SCRIPTS = BUNDLE.templates["en-IN"].scripts
NOW = datetime.now(UTC)
SENTINEL = "my PAN is ABCDE1234F and I live at 42 Sentinel Lane"


class FakeConn:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self.commits = self.rollbacks = 0

    def execute(self, sql: str, params: Any = None) -> "FakeConn":
        self.sql.append(sql)
        return self

    def fetchall(self) -> list[Any]:
        return []  # no stored slot rows (store.latest_slots)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


class Recorder:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[dict[str, Any]] = []
        monkeypatch.setattr(audit_chain, "append", self.append)

    def append(self, conn: Any, keys: Any, **event: Any) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e["event_type"].value for e in self.events]


def row(**update: Any) -> SessionRow:
    base = SessionRow(
        session_id=SID,
        subject_ref=SUBJECT,
        key_ref="key-ref",
        channel="web",
        locale="en-IN",
        fsm_state="S0",
        frame_stack=[],
        pins=pins().model_dump(mode="json"),
        status="active",
        created_at=NOW,
        last_activity_at=NOW,
        expires_at=NOW + timedelta(days=30),
        token_sha256=bytes(32),
        consent_id=None,
        counters={},
    )
    return SessionRow(**{**base.__dict__, **update})


def session(**update: Any) -> SessionState:
    base = SessionState(
        session_id=SID, subject_ref=str(SUBJECT), fsm_state=FsmState.S0, pins=pins()
    )
    return base.model_copy(update=update)


def turn(models: Models | None = None, *, text: str | None = "hello", **config: Any) -> Turn:
    cfg = settings(**config)
    t = Turn(
        settings=cfg,
        conn=FakeConn(),  # type: ignore[arg-type]
        keys=None,  # type: ignore[arg-type]
        gateway=gateway(models or Models(), cfg),
        domain=domain(),
        gate=FakeGate(),  # type: ignore[arg-type]
        pack=load_pack(cfg.output_lexicon),
        session_id=SID,
        turn_key=KEY,
        text=text,
        action=None if text else {"type": "QUICK_REPLY", "payload": {}},
    )
    t.row, t.bundle, t.next = row(), BUNDLE, session()
    t.in_id, t.out_id, t.in_seq = UUID(int=1), UUID(int=2), 1
    return t


def rt(t: Turn) -> Any:
    return SimpleNamespace(context=t)


async def run(t: Turn) -> None:
    """input -> route -> (routed handler | side query | objection | timer | state node) -> decide
    -> [entered handler] -> compose -> validate. The side query and the objection run the state
    node themselves (Step 22)."""
    state = GraphState()
    await nodes.input_node(state, rt(t))
    goto = (await nodes.route(state, rt(t))).goto
    steps = {"side_query": nodes.side_query, "objection": nodes.objection_node}
    if goto in steps:
        await steps[str(goto)](state, rt(t))
    elif goto == "timer":
        await nodes.timer(state, rt(t))
    elif goto in nodes.ROUTED:
        await nodes.ROUTED[str(goto)](state, runtime=rt(t))
    else:
        fsm_state = FsmState(goto)
        await states.wrapped(fsm_state, states.NODES[fsm_state])(state, runtime=rt(t))
    after = (await nodes.decide(state, rt(t))).goto
    if after != "compose":
        await nodes.ENTERED[str(after)](state, runtime=rt(t))
    await nodes.compose(state, rt(t))
    await nodes.validate(state, rt(t))


def pipeline(analysis: TurnAnalysis | None, block: str | None = None) -> PipelineResult:
    return PipelineResult(
        analysis=analysis,
        stored_raw="x",
        redacted="x",
        language="en",
        overlong=False,
        reminder=False,
        blocked=block is not None,
        block_reason=block,  # type: ignore[arg-type]
    )


# --- the §2.6 turn router -------------------------------------------------------------------------
def test_the_router_honours_a_withdrawal_first_then_safety_then_side_queries() -> None:
    every = TurnAnalysis(
        intents=[Intent.SAFETY, Intent.META_WITHDRAW], side_query="80C?", language="en"
    )
    assert turn_router(session(), pipeline(every), 2) == ("withdraw_consent", None)

    safety = TurnAnalysis(intents=[Intent.SAFETY], side_query="80C?", language="en")
    assert turn_router(session(), pipeline(safety), 2) == ("safety", None)
    assert turn_router(session(), pipeline(None, "safety"), 2) == ("safety", None)

    side = TurnAnalysis(intents=[], side_query="80C?", language="en")
    goto, frame = turn_router(session(pending_slot="age"), pipeline(side), 2)
    assert goto == "side_query" and frame == Frame(state=FsmState.S0, pending_slot="age")

    # Step 22: a full stack still reaches the subgraph, without a frame: the brief answer.
    full = session(stack=[Frame(state=FsmState.S0), Frame(state=FsmState.S0)])
    assert turn_router(full, pipeline(side), 2) == ("side_query", None)
    # ... and an objection comes before a side question; a question the router read from the turn
    # (beyond nlu-extract's side_query) is a side question too.
    assert turn_router(session(), pipeline(side), 2, objecting=True) == ("objection", None)
    assert turn_router(session(), pipeline(None), 2, question="is it recorded?")[0] == "side_query"
    assert turn_router(session(fsm_state=FsmState.S2), None, 2) == ("S2", None)


# --- a whole template turn ------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_template_turn_is_audited_from_input_to_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    t = turn()

    await run(t)

    types = recorder.types()
    assert types[:6] == ["TURN_INPUT"] + ["GUARD_VERDICT"] * 4 + ["STATE_TRANSITION"]
    assert set(types[6:]) == {"GUARD_VERDICT"}  # the release checks (rail 8)
    transition = recorder.events[5]["header"]
    assert (transition.from_state, transition.to_state) == ("S0", "S0")
    assert transition.trigger == "S0.STAY"
    # S0's first turn is the greeting (Step 18): the template, the registry's AI disclosure and the
    # notice, verbatim, with the consent form.
    assert t.released is not None and t.released.rendered is not None
    assert [i for i, _ in t.released.rendered.parts] == [
        "template:greeting",
        "registry:DISC-GLOBAL-AI-06",
        "notice:2026.09.1-en",
    ]
    assert t.form is not None and t.form["notice_version"] == "2026.09.1-en"
    assert t.next is not None and t.next.counters == {"low_confidence_streak": 0}
    statuses = [d["status"] for e, d in t.gate.published if e == "turn.status"]  # type: ignore[attr-defined]
    assert statuses == ["analysing", "composing", "validating"]


@pytest.mark.asyncio
async def test_a_withdrawal_reaches_its_handler_and_data_erasure_in_the_same_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    t = turn(Models(intents=("META_WITHDRAW",)))

    await run(t)

    assert t.routed == "withdraw_consent"
    assert t.transition is not None and t.transition.to is FsmState.DATA_ERASURE
    header = next(
        e["header"] for e in recorder.events if e["event_type"] is EventType.STATE_TRANSITION
    )
    assert header.trigger == "CC1" and header.invariants["I5"] is True
    # No consent in S0: nothing to withdraw, and the live data still goes after the commit.
    erasure = next(e for e in recorder.events if e["event_type"] is EventType.ERASURE_REQUEST)
    assert erasure["header"].consent_withdrawal == "none"
    assert t.erasure == "WITHDRAW"
    assert t.released is not None and t.released.text == SCRIPTS.erasure_done


@pytest.mark.asyncio
async def test_a_safety_signal_answers_with_the_crisis_template_and_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)
    t = turn(Models(safety="unsafe"))

    await run(t)

    assert t.routed == "safety"
    # The crisis script leads, and the escalation's own message follows in the same turn: in S0,
    # with no consent, contact options only.
    assert t.released is not None
    assert t.released.text == f"{SCRIPTS.safety}\n\n{SCRIPTS.contact_options}"
    assert t.transition is not None and t.transition.to is FsmState.HUMAN_ESCALATION
    assert t.transition.reason_code == "HE_SAFETY"


@pytest.mark.asyncio
async def test_an_overlong_turn_is_asked_to_shorten(monkeypatch: pytest.MonkeyPatch) -> None:
    Recorder(monkeypatch)
    t = turn(text="word " * 600)
    t.next = session(last_prompt_id="s0.consent")  # the greeting was shown

    await run(t)

    assert t.released is not None and t.released.text == SCRIPTS.ask_to_shorten


@pytest.mark.asyncio
async def test_an_action_turn_is_audited_without_rails(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(monkeypatch)
    models = Models()
    t = turn(models, text=None)

    await run(t)

    assert recorder.types()[:2] == ["TURN_INPUT", "STATE_TRANSITION"]
    assert recorder.events[0]["payload"] == {"action": {"type": "QUICK_REPLY", "payload": {}}}
    assert models.routes == []  # no guard, no nlu: there is no free text


@pytest.mark.asyncio
async def test_a_side_query_pushes_a_frame_the_subgraph_pops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)
    t = turn(Models(side_query="how does 80C work?"))
    state = GraphState()
    await nodes.input_node(state, rt(t))

    assert (await nodes.route(state, rt(t))).goto == "side_query"
    assert t.next is not None and len(t.next.stack) == 1 and t.frame == t.next.stack[0]
    await nodes.side_query(state, rt(t))
    assert t.next.stack == [] and t.side_outcome is not None

    full = turn(Models(side_query="how does 80C work?"), side_query_max_stack=0)
    await nodes.input_node(state, rt(full))
    assert (await nodes.route(state, rt(full))).goto == "side_query"
    assert full.side_full and full.frame is None and full.next is not None and not full.next.stack


@pytest.mark.asyncio
async def test_injection_hits_are_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    Recorder(monkeypatch)
    t = turn(Models(injection=0.99))
    t.next = session(counters={"injection": 2})

    await run(t)

    assert t.next.counters["injection"] == 3
    assert t.transition is not None and t.transition.reason_code == "HE_INJECTION"


# --- dependency down ------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_domain_outage_degrades_the_reply_and_a_contract_error_does_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)

    async def down(state: GraphState, *, runtime: Any) -> None:
        raise DomainError("UNAVAILABLE", None)

    async def refused(state: GraphState, *, runtime: Any) -> None:
        raise DomainError("RULES_VERSION_UNKNOWN", 409)

    t = turn()
    await states.guarded("S1", down)(GraphState(), runtime=rt(t))
    await nodes.compose(GraphState(), rt(t))
    assert t.degraded
    assert t.render is not None and t.render(None).text == SCRIPTS.release_blocked

    with pytest.raises(DomainError):
        await states.guarded("S1", refused)(GraphState(), runtime=rt(turn()))


# --- load -----------------------------------------------------------------------------------------
def loading(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stored: Any = None,
    conv_seq: int = 0,
    switches: set[tuple[str, str]] | None = None,
    current: SessionRow | None = None,
) -> None:
    monkeypatch.setattr(store, "get_session", lambda conn, sid, lock=False: current or row())
    monkeypatch.setattr(store, "released_response", lambda conn, keys, sid, key: stored)
    monkeypatch.setattr(store, "last_seq", lambda conn, sid: conv_seq)
    monkeypatch.setattr(store, "active_kill_switches", lambda conn: switches or set())


def released_body(
    text: str = "Would you like a licensed advisor to contact you?",
) -> dict[str, Any]:
    return {"turn_id": "x", "state": "S0", "message": {"text": text}, "documents": []}


@pytest.mark.asyncio
async def test_load_replays_a_released_turn_without_running_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    body = released_body()
    loading(monkeypatch, stored=body)
    t = turn()
    t.gate.idem[KEY] = hashlib.sha256(body["message"]["text"].encode()).hexdigest()  # type: ignore[attr-defined]

    command = await nodes.load(GraphState(), rt(t))

    assert command.goto == END
    assert t.replayed and t.response == body and t.conn.rollbacks == 1  # type: ignore[attr-defined]

    t.gate.idem[KEY] = "0" * 64  # type: ignore[attr-defined]
    with caplog.at_level(logging.ERROR, logger="surakshasetu.graph.nodes"):
        await nodes.load(GraphState(), rt(t))
    assert "idempotency hint differs" in caplog.text


@pytest.mark.asyncio
async def test_a_hint_without_a_committed_turn_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    loading(monkeypatch, stored=None)
    t = turn()
    t.gate.idem[KEY] = "0" * 64  # type: ignore[attr-defined]

    with pytest.raises(LookupError):
        await nodes.load(GraphState(), rt(t))


@pytest.mark.asyncio
async def test_load_hydrates_from_conv_only_when_the_checkpoint_lags(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    checkpointed = session(fsm_state=FsmState.S1).model_dump(mode="json")
    loading(monkeypatch, conv_seq=2)

    current = turn()
    command = await nodes.load(GraphState(session=checkpointed, committed_seq=2), rt(current))
    assert command.goto == "input"
    assert not current.hydrated and current.next is not None
    assert current.next.fsm_state is FsmState.S1 and current.in_seq == 3

    lagging = turn()
    with caplog.at_level(logging.WARNING, logger="surakshasetu.graph.nodes"):
        await nodes.load(GraphState(session=checkpointed, committed_seq=0), rt(lagging))
    assert lagging.hydrated and lagging.next is not None
    assert lagging.next.fsm_state is FsmState.S0  # conv.session's state, not the checkpoint's
    assert "checkpoint lags conv" in caplog.text


@pytest.mark.asyncio
async def test_a_kill_switch_on_the_active_bundle_refuses_the_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loading(monkeypatch, switches={("prompt_bundle", "pb-2026.10.8")})

    with pytest.raises(BundleError) as excinfo:
        await nodes.load(GraphState(), rt(turn()))
    assert excinfo.value.reason == "KILL_SWITCHED"


# --- commit and release ---------------------------------------------------------------------------
class StoreCalls:
    def __init__(self, monkeypatch: pytest.MonkeyPatch, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        for name in ("insert_turn", "insert_slot", "update_session"):
            monkeypatch.setattr(store, name, self._record(name, fail_on))

    def _record(self, name: str, fail_on: str | None) -> Any:
        def call(conn: Any, *args: Any, **kwargs: Any) -> None:
            if name == fail_on:
                raise RuntimeError("database down")
            self.calls.append((name, kwargs))

        return call


@pytest.mark.asyncio
async def test_commit_writes_the_turn_pair_and_the_release_then_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    writes = StoreCalls(monkeypatch)
    t = turn()
    await run(t)

    update = await nodes.commit(GraphState(), rt(t))

    names = [name for name, _ in writes.calls]
    assert names == ["insert_turn", "insert_turn", "update_session"]
    turn_in, turn_out = writes.calls[0][1], writes.calls[1][1]
    assert (turn_in["seq"], turn_in["direction"], turn_in["turn_key"]) == (1, "in", KEY)
    assert (turn_out["seq"], turn_out["direction"]) == (2, "out")
    assert turn_out["turn_key"] == uuid5(KEY, "out")
    assert turn_in["analysis"] == {
        "intents": [],
        "language": "en",
        "slots": [],
        "has_side_query": False,
    }
    released = recorder.events[-1]
    assert released["event_type"] is EventType.RESPONSE_RELEASED
    assert (
        released["header"].rendered_sha256 == hashlib.sha256(t.released.text.encode()).hexdigest()  # type: ignore[union-attr]
    )
    assert released["payload"] == {"response": t.response}
    assert t.conn.commits == 1  # type: ignore[attr-defined]
    assert update["committed_seq"] == 2 and update["session"]["fsm_state"] == "S0"


@pytest.mark.asyncio
async def test_a_failed_commit_releases_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    Recorder(monkeypatch)
    StoreCalls(monkeypatch, fail_on="update_session")
    t = turn()
    await run(t)

    with pytest.raises(RuntimeError):
        await nodes.commit(GraphState(), rt(t))
    assert t.response is None and t.conn.commits == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_slot_row_without_consent_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    Recorder(monkeypatch)
    StoreCalls(monkeypatch)
    t = turn()
    await run(t)
    t.slot_rows = [SlotRow("age_years", 34, 0.95, "confirmed")]

    with pytest.raises(RuntimeError, match="I1"):
        await nodes.commit(GraphState(), rt(t))


@pytest.mark.asyncio
async def test_release_stores_the_hint_and_publishes_after_the_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)
    StoreCalls(monkeypatch)
    t = turn()
    await run(t)
    await nodes.commit(GraphState(), rt(t))

    await nodes.release_node(GraphState(), rt(t))

    gate = t.gate
    released = t.released.text  # type: ignore[union-attr]
    assert gate.idem[KEY] == hashlib.sha256(released.encode()).hexdigest()  # type: ignore[attr-defined]
    assert gate.published[-1] == ("turn.released", t.response)  # type: ignore[attr-defined]

    gate.down = True  # type: ignore[attr-defined]
    await nodes.release_node(GraphState(), rt(t))  # a Redis outage after the commit only warns


def test_session_status_follows_the_state() -> None:
    assert nodes.session_status(FsmState.S2) == "active"
    assert nodes.session_status(FsmState.PAUSE) == "paused"
    assert nodes.session_status(FsmState.EXIT) == "ended"


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
async def test_no_customer_words_reach_the_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", tmp_path)
    Recorder(monkeypatch)
    StoreCalls(monkeypatch)
    t = turn(Models(side_query="does 80C apply to 42 Sentinel Lane?"), text=SENTINEL)

    await run(t)
    await nodes.commit(GraphState(), rt(t))
    await nodes.release_node(GraphState(), rt(t))

    logged = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "transition S0 -> S0" in logged and "turn released" in logged
    for leaked in ("ABCDE1234F", "Sentinel Lane", "key-ref", str(KEY)):
        assert leaked not in logged
