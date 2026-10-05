"""The cross-cutting handlers (Step 17) with fakes: no database, no network. They run through the
same node sequence as a real turn (test_runtime_nodes.run). Audit appends are recorded, the domain
tier is an in-memory fake, and the store's SQL is replaced where a handler reaches it. The db tests
run the same handlers for real (tests/integration/test_cross_cutting.py)."""

import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from runtime_support import REQUIRED_SLOTS, Models, ai_disclosure_json, notice_json
from test_runtime_nodes import (
    SCRIPTS,
    Recorder,
    StoreCalls,
    rt,
    run,
    session,
    turn,
)

from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.chain import AuditEvent
from surakshasetu.audit.events import EventType
from surakshasetu.domain.client import DomainError
from surakshasetu.domain.models import (
    ConsentNotice,
    ConsentRecord,
    Disclosure,
    PurposeGrant,
    RecommendedOption,
    RequiredSlot,
)
from surakshasetu.fsm import rows
from surakshasetu.fsm.facts import MandatoryTrigger
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import nodes
from surakshasetu.graph.facts import build_facts
from surakshasetu.graph.handlers import data_erasure, human_escalation, pause
from surakshasetu.graph.nodes import Turn, turn_router
from surakshasetu.graph.state import Frame, GraphState, RecommendationPayload
from surakshasetu.logging import configure_logging
from surakshasetu.store import conv as store

CONSENT_ID = UUID("0199a1b2-0000-7000-8000-00000000c0c0")
HANDOFF_ID = UUID("0199a1b2-0000-7000-8000-00000000ab0f")
PII = "my PAN is ABCDE1234F, call 9876543210"


def consent(*purposes: str, adult: bool = True, **update: Any) -> ConsentRecord:
    granted = set(purposes or ("P1",))
    record = ConsentRecord(
        consent_id=CONSENT_ID,
        notice_version="2026.09.1-en",
        notice_sha256="ab" * 32,
        notice_language="en-IN",
        ai_disclosure_version="2026.09.1",
        purposes=[
            PurposeGrant(purpose_id=p, granted=p[:2] in granted)  # type: ignore[arg-type]
            for p in ("P1_NEEDS_RECO", "P2_ADVISOR_CONTACT", "P3_MARKETING")
        ],
        age_18_plus_declared=adult,
        method="structured_action",
        captured_at=datetime.now(UTC),
        valid_p1=adult and "P1" in granted,
        valid_reasons=[] if adult else ["AGE_NOT_DECLARED"],
    )
    return record.model_copy(update=update)


class FakeDomain:
    """The Consent Service and catalog calls the handlers make. `down` fails every call as an
    outage does."""

    def __init__(self, *, down: bool = False, record: ConsentRecord | None = None) -> None:
        self.down, self.record = down, record
        self.calls: list[str] = []
        self.products: dict[str, Any] = {}

    def _call(self, name: str) -> None:
        self.calls.append(name)
        if self.down:
            raise DomainError("UNAVAILABLE", None)

    async def withdraw_consent(self, consent_id: UUID, withdrawal: Any) -> ConsentRecord:
        self._call("withdrawConsent")
        return consent(withdrawn_at=datetime.now(UTC), valid_p1=False, valid_reasons=["WITHDRAWN"])

    async def change_consent_purpose(self, consent_id: UUID, grant: PurposeGrant) -> ConsentRecord:
        self._call("changeConsentPurpose")
        return consent("P1", "P2")

    async def get_consent_record(self, consent_id: UUID, as_of: Any = None) -> ConsentRecord:
        self._call("getConsentRecord")
        return self.record or consent()

    async def get_required_slots(self, rules: str) -> list[RequiredSlot]:
        self._call("getRequiredSlots")
        return [RequiredSlot.model_validate(r) for r in REQUIRED_SLOTS]

    async def list_products(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []  # I3's catalog names, for S2's generated sentence (not a recorded call)

    async def get_product(self, uin: str, as_of: Any = None) -> Any:
        self._call("getProduct")
        return self.products.get(uin, SimpleNamespace(status="in_force", effective_to=None))

    async def get_current_consent_notice(self, language: str) -> ConsentNotice:
        self._call("getCurrentConsentNotice")
        return ConsentNotice.model_validate(notice_json(language))

    async def get_disclosure(
        self, disclosure_id: str, language: str, as_of: Any = None
    ) -> Disclosure:
        self._call("getDisclosure")
        return Disclosure.model_validate(ai_disclosure_json(language))


def with_consent(t: Turn, record: ConsentRecord | None, state: FsmState = FsmState.S1) -> Turn:
    t.next = session(consent=record, fsm_state=state)
    t.from_state = state  # as load sets it
    return t


def events(recorder: Recorder, event_type: EventType) -> list[dict[str, Any]]:
    return [e for e in recorder.events if e["event_type"] is event_type]


@pytest.fixture
def handoffs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """store.insert_handoff recorded; the briefing's reads (slots, the chain) faked."""
    made: list[dict[str, Any]] = []

    def insert(conn: Any, keys: Any, key_ref: str, **row: Any) -> UUID:
        made.append(row)
        return HANDOFF_ID

    monkeypatch.setattr(store, "insert_handoff", insert)
    monkeypatch.setattr(store, "has_handoff", lambda conn, sid: bool(made))
    monkeypatch.setattr(store, "current_slots", lambda *a: {"note": PII, "age": 34})
    monkeypatch.setattr(audit_chain, "events", lambda conn, sid: [])
    return made


# --- routing --------------------------------------------------------------------------------------
def test_after_decide_runs_the_handler_of_the_state_entered() -> None:
    def case(before: FsmState, to: FsmState, routed: str = "S1") -> str:
        t = turn()
        t.routed, t.transition = routed, SimpleNamespace(to=to)  # type: ignore[assignment]
        return nodes.after_decide(t, before)

    assert case(FsmState.S1, FsmState.DATA_ERASURE) == "data_erasure"
    assert case(FsmState.S1, FsmState.HUMAN_ESCALATION) == "human_escalation"
    assert case(FsmState.S3, FsmState.PAUSE) == "pause"
    assert case(FsmState.S2, FsmState.S3) == "s3_enter"  # Step 21: the recommendation
    assert case(FsmState.S1, FsmState.S2) == "s2_enter"  # Step 20: S2's first question
    # Staying in a handler's state runs that state's node instead, never the entry again.
    assert case(FsmState.HUMAN_ESCALATION, FsmState.HUMAN_ESCALATION) == "compose"
    assert case(FsmState.PAUSE, FsmState.PAUSE) == "compose"
    # I5 in a closed state: the FSM stays, the router's withdrawal still erases.
    closed = FsmState.HUMAN_ESCALATION
    assert case(closed, closed, routed="withdraw_consent") == "data_erasure"


def test_the_erase_action_is_a_withdrawal_for_the_router_and_the_facts() -> None:
    erase = {"type": "ERASE", "payload": {}}
    assert turn_router(session(), None, 2, erase) == ("withdraw_consent", None)
    assert turn_router(session(), None, 2, {"type": "QUICK_REPLY"}) == ("S0", None)
    settings = turn().settings
    assert build_facts(session(), None, None, settings, action_type="ERASE").withdraw
    assert not build_facts(session(), None, None, settings, action_type="QUICK_REPLY").withdraw


def test_every_escalation_reason_has_a_queue() -> None:
    every = [*rows.CROSS_CUTTING, *(r for table in rows.STATE_ROWS.values() for r in table)]
    emitted = {r.reason_code for r in every if isinstance(r.reason_code, str)}
    emitted = {code for code in emitted if code.startswith("HE_")}
    emitted |= {t.value for t in MandatoryTrigger} | {"HE_REQUEST", "HE_FRUSTRATION"}
    emitted |= {"HE_LOW_CONFIDENCE", "HE_ELIGIBILITY", "HE_SUITABILITY"}
    assert emitted <= human_escalation.REASON_CODES
    assert human_escalation.queue_for("HE_SAFETY") == "care"
    assert human_escalation.queue_for("HE_REQUEST") == "advisor"


# --- data erasure ---------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_withdrawal_with_consent_is_recorded_by_the_consent_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    t = with_consent(turn(Models(intents=("META_WITHDRAW",))), consent())
    t.domain = FakeDomain()  # type: ignore[assignment]

    await run(t)

    request = events(recorder, EventType.ERASURE_REQUEST)[0]["header"]
    assert (request.reason_code, request.consent_withdrawal) == ("WITHDRAW", "done")
    assert request.consent_id == CONSENT_ID
    withdrawn = events(recorder, EventType.CONSENT_WITHDRAWN)[0]["header"]
    assert withdrawn.purposes == ["P1"]
    assert t.erasure == "WITHDRAW"
    assert t.released is not None and t.released.text == SCRIPTS.erasure_done
    assert t.transition is not None and t.transition.row_id == "CC1"


@pytest.mark.asyncio
async def test_a_consent_service_outage_still_erases_and_never_says_withdrawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    t = with_consent(turn(Models(intents=("META_WITHDRAW",))), consent())
    t.domain = FakeDomain(down=True)  # type: ignore[assignment]

    await run(t)

    request = events(recorder, EventType.ERASURE_REQUEST)[0]["header"]
    assert request.consent_withdrawal == "pending"
    assert events(recorder, EventType.CONSENT_WITHDRAWN) == []
    assert t.erasure == "WITHDRAW"  # still deleted after the commit
    assert t.released is not None and t.released.text == SCRIPTS.erasure_pending
    assert "withdrawn" not in SCRIPTS.erasure_pending.lower()


@pytest.mark.asyncio
async def test_a_minor_is_erased_with_the_polite_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = Recorder(monkeypatch)
    t = with_consent(turn(), consent(adult=False))  # P1 granted, 18+ unticked: V2
    t.domain = FakeDomain()  # type: ignore[assignment]

    await run(t)

    assert t.transition is not None and t.transition.row_id == "CC1b"
    assert events(recorder, EventType.ERASURE_REQUEST)[0]["header"].reason_code == "MINOR"
    assert t.erasure == "MINOR"
    assert t.released is not None and t.released.text == SCRIPTS.minor_exit


@pytest.mark.asyncio
async def test_a_withdrawal_in_a_closed_state_still_erases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    t = with_consent(turn(Models(intents=("META_WITHDRAW",))), consent(), FsmState.HUMAN_ESCALATION)
    t.domain = FakeDomain()  # type: ignore[assignment]

    await run(t)

    transition = events(recorder, EventType.STATE_TRANSITION)[0]["header"]
    assert transition.to_state == "HUMAN_ESCALATION" and transition.invariants["I5"] is False
    assert events(recorder, EventType.ERASURE_REQUEST) and t.erasure == "WITHDRAW"
    assert t.released is not None and t.released.text == SCRIPTS.erasure_done


@pytest.mark.asyncio
async def test_safety_leads_even_when_a_withdrawal_wins_the_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)
    t = turn(Models(intents=("META_WITHDRAW",), safety="unsafe S11"))

    await run(t)

    assert t.routed == "withdraw_consent" and t.safety
    assert t.released is not None
    assert t.released.text == f"{SCRIPTS.safety}\n\n{SCRIPTS.erasure_done}"


@pytest.mark.asyncio
async def test_commit_marks_an_erasure_turn_erased(monkeypatch: pytest.MonkeyPatch) -> None:
    Recorder(monkeypatch)
    writes = StoreCalls(monkeypatch)
    t = turn(Models(intents=("META_WITHDRAW",)))
    await run(t)

    await nodes.commit(GraphState(), rt(t))

    update = dict(writes.calls)["update_session"]
    assert update["status"] == "erased" and update["fsm_state"] == "DATA_ERASURE"


def test_erase_session_deletes_children_first_then_the_checkpoint() -> None:
    class Conn:
        def __init__(self) -> None:
            self.sql: list[str] = []

        def execute(self, sql: str, params: Any) -> Any:
            self.sql.append(sql)
            assert params == {"session": CONSENT_ID, "thread": str(CONSENT_ID)}
            return SimpleNamespace(rowcount=1)

    conn = Conn()

    class Pool:
        def connection(self) -> Any:
            class Ctx:
                def __enter__(self) -> Conn:
                    return conn

                def __exit__(self, *exc: Any) -> None:
                    pass

            return Ctx()

    counts = data_erasure.erase_session(Pool(), CONSENT_ID)  # type: ignore[arg-type]

    tables = [re.search(r"DELETE FROM (\S+)", sql).group(1) for sql in conn.sql]  # type: ignore[union-attr]
    assert tables == [
        "conv.disclosure_ack",
        "conv.recommendation",
        "conv.handoff",
        "conv.slot_value",
        "conv.turn",
        "conv.session",
        "langgraph.checkpoint_writes",
        "langgraph.checkpoint_blobs",
        "langgraph.checkpoints",
    ]
    assert counts == (6, 3)
    assert not any("audit" in sql for sql in conn.sql)


def test_erase_schedules_the_key_or_destroys_a_minors_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(data_erasure, "erase_session", lambda pool, sid: (6, 3))
    keys = SimpleNamespace(scheduled=[], destroyed=[])
    keys.schedule_destruction = lambda ref, after: keys.scheduled.append((ref, after))
    keys.destroy = lambda ref: keys.destroyed.append(ref)
    settings = turn().settings

    data_erasure.erase(None, keys, settings, session_id=CONSENT_ID, key_ref="k", minor=False)  # type: ignore[arg-type]
    data_erasure.erase(None, keys, settings, session_id=CONSENT_ID, key_ref="m", minor=True)  # type: ignore[arg-type]

    ref, after = keys.scheduled[0]
    expected = datetime.now(UTC) + timedelta(days=397)
    assert ref == "k" and abs((after - expected).total_seconds()) < 60
    assert keys.destroyed == ["m"]


# --- human escalation -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_without_consent_an_escalation_gives_contact_options_and_shares_nothing(
    monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
) -> None:
    recorder = Recorder(monkeypatch)
    t = turn(Models(intents=("META_HUMAN",)))

    await run(t)

    assert t.transition is not None and t.transition.reason_code == "HE_REQUEST"
    assert t.released is not None and t.released.text == SCRIPTS.contact_options
    assert handoffs == [] and events(recorder, EventType.HANDOFF) == []


@pytest.mark.asyncio
async def test_p2_is_asked_before_any_data_is_shared(
    monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
) -> None:
    recorder = Recorder(monkeypatch)
    t = with_consent(turn(Models(intents=("META_HUMAN",))), consent("P1"))

    await run(t)

    assert t.released is not None and t.released.text == SCRIPTS.advisor_consent_ask
    assert handoffs == [] and events(recorder, EventType.HANDOFF) == []
    assert [q["action"] for q in t.quick_replies] == [
        {"type": "ADVISOR_CONTACT", "payload": {"granted": True}},
        {"type": "ADVISOR_CONTACT", "payload": {"granted": False}},
    ]


@pytest.mark.asyncio
async def test_with_p2_the_escalation_hands_off_a_redacted_briefing(
    monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
) -> None:
    recorder = Recorder(monkeypatch)
    t = with_consent(turn(Models(intents=("META_HUMAN",))), consent("P1", "P2"))

    await run(t)

    assert t.released is not None and t.released.text == SCRIPTS.handoff
    (made,) = handoffs
    assert (made["reason_code"], made["queue"]) == ("HE_REQUEST", "advisor")
    handoff = events(recorder, EventType.HANDOFF)[0]
    assert handoff["header"].handoff_id == HANDOFF_ID
    briefing = made["payload"]
    assert handoff["payload"] == {"briefing": briefing}
    assert briefing["reason_code"] == "HE_REQUEST" and briefing["consent_purposes"] == ["P1", "P2"]
    assert briefing["profile"]["age"] == 34
    flat = str(briefing)
    assert "ABCDE1234F" not in flat and "9876543210" not in flat
    assert "<PAN_1>" in briefing["profile"]["note"]


@pytest.mark.asyncio
async def test_a_closed_queue_still_queues_and_shows_contact_options(
    monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
) -> None:
    Recorder(monkeypatch)
    t = with_consent(
        turn(Models(safety="unsafe S11"), advisor_queue_open=False), consent("P1", "P2")
    )

    await run(t)

    assert handoffs[0]["queue"] == "care" and handoffs[0]["reason_code"] == "HE_SAFETY"
    assert t.released is not None
    assert t.released.text == f"{SCRIPTS.safety}\n\n{SCRIPTS.contact_options}"


def he_turn(action: dict[str, Any] | None, record: ConsentRecord | None) -> Turn:
    t = with_consent(turn(text=None), record, FsmState.HUMAN_ESCALATION)
    t.action = action
    t.domain = FakeDomain()  # type: ignore[assignment]
    return t


@pytest.mark.asyncio
async def test_the_p2_answer_records_the_grant_then_hands_off_once(
    monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
) -> None:
    recorder = Recorder(monkeypatch)
    entered = AuditEvent(
        UUID(int=9), UUID(int=1), 3, "STATE_TRANSITION", datetime.now(UTC), "S1", {},
        {"from_state": "S1", "to_state": "HUMAN_ESCALATION", "trigger": "CC2",
         "invariants": {}, "reason_code": "HE_FRUSTRATION"},
        b"", "k", b"", b"",
    )  # fmt: skip
    monkeypatch.setattr(audit_chain, "events", lambda conn, sid: [entered])
    grant = {"type": "ADVISOR_CONTACT", "payload": {"granted": True}}
    t = he_turn(grant, consent("P1"))

    await run(t)

    assert t.domain.calls == ["changeConsentPurpose"]  # type: ignore[attr-defined]
    captured = events(recorder, EventType.CONSENT_CAPTURED)[0]["header"]
    assert captured.purposes == ["P1", "P2"] and captured.method == "structured_action"
    assert captured.language == "en-IN" and captured.adult_declared is True
    assert captured.captured_at is not None
    assert handoffs[0]["reason_code"] == "HE_FRUSTRATION"
    assert t.released is not None and t.released.text == SCRIPTS.handoff

    again = he_turn(grant, consent("P1", "P2"))
    await run(again)
    assert len(handoffs) == 1 and again.domain.calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_declined_or_unconsented_p2_answer_gets_contact_options(
    monkeypatch: pytest.MonkeyPatch, handoffs: list[dict[str, Any]]
) -> None:
    Recorder(monkeypatch)
    declined = he_turn({"type": "ADVISOR_CONTACT", "payload": {"granted": False}}, consent("P1"))
    no_consent = he_turn({"type": "ADVISOR_CONTACT", "payload": {"granted": True}}, None)

    for t in (declined, no_consent):
        await run(t)
        assert t.released is not None and t.released.text == SCRIPTS.contact_options
        assert t.domain.calls == []  # type: ignore[attr-defined]
    assert handoffs == []


# --- pause and resume -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_entering_pause_answers_with_the_paused_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)
    t = turn()
    t.next = session(fsm_state=FsmState.S2)
    t.transition = SimpleNamespace(to=FsmState.PAUSE)  # type: ignore[assignment]

    await pause.pause(GraphState(), runtime=rt(t))
    await nodes.compose(GraphState(), rt(t))

    assert t.render is not None and t.render(None).text == SCRIPTS.paused


def paused(record: ConsentRecord | None) -> Turn:
    t = turn()
    t.next = session(consent=record, fsm_state=FsmState.PAUSE, stack=[Frame(state=FsmState.S2)])
    return t


@pytest.mark.asyncio
async def test_resume_with_valid_consent_returns_to_the_paused_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)
    t = paused(consent())
    t.domain = FakeDomain()  # type: ignore[assignment]

    await run(t)

    # Step 20: entering S2 again asks its open question (the rules' slots are read).
    assert t.domain.calls == ["getConsentRecord", "getRequiredSlots"]  # type: ignore[attr-defined]
    assert t.transition is not None and t.transition.row_id == "PAUSE.R"
    assert t.next is not None and t.next.fsm_state is FsmState.S2


@pytest.mark.asyncio
async def test_resume_with_lapsed_consent_re_enters_s0(monkeypatch: pytest.MonkeyPatch) -> None:
    Recorder(monkeypatch)
    t = paused(consent())
    expired = consent(valid_p1=False, valid_reasons=["CONSENT_EXPIRED"])
    t.domain = FakeDomain(record=expired)  # type: ignore[assignment]

    await run(t)

    assert t.transition is not None and t.transition.row_id == "G1"
    assert t.next is not None and t.next.fsm_state is FsmState.S0


@pytest.mark.asyncio
async def test_resume_fails_closed_when_consent_cannot_be_revalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Recorder(monkeypatch)
    t = paused(consent())
    t.domain = FakeDomain(down=True)  # type: ignore[assignment]

    with pytest.raises(DomainError):
        await run(t)
    assert not t.degraded


def recommendation(valid_until: date) -> RecommendationPayload:
    option = RecommendedOption.model_construct(
        uin="999N001V02", quote=SimpleNamespace(valid_until=valid_until)
    )
    return RecommendationPayload.model_construct(options=[option])


@pytest.mark.asyncio
async def test_resume_drops_a_stale_recommendation(monkeypatch: pytest.MonkeyPatch) -> None:
    today = datetime.now(pause.IST).date()

    async def resumed(t: Turn) -> bool:
        await pause.resume(GraphState(), runtime=rt(t))
        assert t.next is not None
        return t.next.recommendation is None

    def fresh(**domain: Any) -> Turn:
        t = paused(consent())
        t.next.recommendation = recommendation(today)  # type: ignore[union-attr]
        t.domain = FakeDomain()  # type: ignore[assignment]
        t.domain.products.update(domain)  # type: ignore[attr-defined]
        return t

    assert not await resumed(fresh())
    expired = fresh()
    expired.next.recommendation = recommendation(today - timedelta(days=1))  # type: ignore[union-attr]
    assert await resumed(expired)
    switched = fresh()
    switched.kill_switches = {("product", "999N001V02")}
    assert await resumed(switched)
    withdrawn = SimpleNamespace(status="withdrawn", effective_to=None)
    assert await resumed(fresh(**{"999N001V02": withdrawn}))


# --- logs -----------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
async def test_the_handlers_log_no_customer_data(
    monkeypatch: pytest.MonkeyPatch,
    handoffs: list[dict[str, Any]],
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG", tmp_path)
    Recorder(monkeypatch)
    escalated = with_consent(turn(Models(intents=("META_HUMAN",)), text=PII), consent("P1", "P2"))
    erased = with_consent(turn(Models(intents=("META_WITHDRAW",)), text=PII), consent())
    erased.domain = FakeDomain(down=True)  # type: ignore[assignment]

    await run(escalated)
    await run(erased)

    logged = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "handed off to advisor" in logged and "consent withdrawal pending" in logged
    for leaked in ("ABCDE1234F", "9876543210", str(CONSENT_ID), str(HANDOFF_ID), "key-ref"):
        assert leaked not in logged
