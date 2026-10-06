"""The side-query subgraph (Step 22, TDD §2.6): frames pushed and popped, the frozen view, the
compound turn's order (the slot first, then the answer, then the resume), a full stack, retrieval
down, the offer after five in a row, S0's privacy FAQ (no generation), tax (the regime condition,
asked when unknown, DISC-GLOBAL-TAX-05), the regime revealed in a question (proposed, then
confirmed), the tax year, the FAQ loader, the RESPONSE_RELEASED header and a log sentinel."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from compose_support import retrieved
from pydantic import ValidationError
from runtime_support import Models
from test_runtime_nodes import BUNDLE, Recorder, StoreCalls, rt, run, session
from test_s0 import s0_turn
from test_s1 import TDD, s1_turn
from test_s2 import s2_turn
from test_s3 import Evidence, tax_evidence

from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import handlers, nodes, side_query
from surakshasetu.graph.nodes import Turn
from surakshasetu.graph.state import Frame, GraphState
from surakshasetu.retrieval.service import RetrievalUnavailable
from surakshasetu.store import conv as store

SCRIPTS = BUNDLE.templates["en-IN"].scripts
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
CITED_TAX = "Premiums qualify for a deduction under the old regime only [E1]."


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


@pytest.fixture(autouse=True)
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, Any]]:
    rows: dict[str, tuple[str, Any]] = {}
    monkeypatch.setattr(store, "latest_slots", lambda *a: dict(rows))
    monkeypatch.setattr(handlers, "_PRODUCT_NAMES", {})
    monkeypatch.setattr(handlers, "now", lambda: NOW)
    return rows


class Down:
    """Retrieval down: every call raises."""

    def __init__(self) -> None:
        self.asked = 0

    async def retrieve(self, query: str, ctx: Any) -> Any:
        self.asked += 1
        raise RetrievalUnavailable("QDRANT_UNAVAILABLE")


def files(logs: Path) -> str:
    return "".join(p.read_text() for p in logs.glob("*.log"))


def ids(t: Turn) -> list[str]:
    assert t.released is not None and t.released.rendered is not None
    return [i for i, _ in t.released.rendered.parts]


def kinds(t: Turn) -> list[str]:
    return [q["action"]["type"] for q in t.quick_replies]


def asked_s1(question: str = "how does 80C work?", **kw: Any) -> Turn:
    models = kw.pop("models", None) or Models(side_query=question, recommend=(CITED_TAX,))
    t = s1_turn(question, models=models, prompt="s1.ask:age_years", **kw)
    t.retrieval = kw.get("retrieval") or Evidence(tax_evidence())
    return t


# --- frames and the frozen view -------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_frame_is_pushed_then_popped_and_the_customer_is_back_where_they_were() -> None:
    t = asked_s1()
    paused = Frame(state=FsmState.S1)  # a frame already on the stack stays
    assert t.next is not None
    t.next.stack = [paused]
    state = GraphState()
    await nodes.input_node(state, rt(t))
    assert (await nodes.route(state, rt(t))).goto == "side_query"
    assert t.next.stack == [paused, t.frame]
    assert t.frame == Frame(state=FsmState.S1, prompt_id="s1.ask:age_years")

    await nodes.side_query(state, rt(t))

    assert t.next.stack == [paused]
    assert t.next.last_prompt_id == "s1.ask:age_years"  # the same question, asked again


def test_the_view_is_frozen_and_nothing_written_to_it_reaches_the_session() -> None:
    current = session(fsm_state=FsmState.S2, counters={"injection": 1}, focus_uins=["999N001V02"])
    view = side_query.view_of(current)

    with pytest.raises(ValidationError):
        view.fsm_state = FsmState.S3  # type: ignore[misc]
    with pytest.raises(ValidationError):
        view.counters = {}  # type: ignore[misc]
    view.counters["injection"] = 9  # a nested write lands on the copy only
    view.focus_uins.append("999N002V01")

    assert current.counters == {"injection": 1} and current.focus_uins == ["999N001V02"]
    assert view.model_dump(exclude={"counters", "focus_uins"}) == current.model_dump(
        exclude={"counters", "focus_uins"}
    )


@pytest.mark.asyncio
async def test_the_answer_reads_the_view_and_leaves_the_session_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t = asked_s1()
    seen: list[Any] = []
    real = side_query.answer

    async def spy(view: Any, io: Any, question: str, **kw: Any) -> Any:
        seen.append(view)
        before = t.next.model_dump()  # type: ignore[union-attr]
        found = await real(view, io, question, **kw)
        assert t.next.model_dump() == before  # type: ignore[union-attr]
        return found

    monkeypatch.setattr(side_query, "answer", spy)
    await run(t)

    [view] = seen
    assert isinstance(view, side_query.SessionView)
    assert not hasattr(handlers.io(t), "next")  # the subgraph's I/O carries no session


# --- the compound turn ----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_compound_turn_applies_the_slot_first_then_answers_then_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    said = "I'm 34, and how does 80C work here?"
    models = Models(
        side_query="how does 80C work here?",
        slots=({"slot": "age_years", "value": 34, "confidence": 0.95, "evidence_span": "34"},),
        recommend=(CITED_TAX,),
    )
    t = asked_s1(said, models=models)
    order: list[str] = []
    real = side_query.answer

    async def spy(view: Any, io: Any, question: str, **kw: Any) -> Any:
        order.append("answer:" + ",".join(r.slot for r in t.slot_rows))
        return await real(view, io, question, **kw)

    monkeypatch.setattr(side_query, "answer", spy)
    await run(t)

    assert order == ["answer:age_years"]  # the age was taken before the question was answered
    assert [(r.slot, r.status) for r in t.slot_rows] == [("age_years", "proposed")]
    parts = ids(t)
    assert parts[:2] == ["generated:answer", "template:side_query_caveat"]
    assert parts[-2:] == ["template:side_query_bridge", "template:RL-S1-RESIDENCY"]
    assert t.next is not None and t.next.last_prompt_id == "s1.ask:residency"  # resumed, moved on
    assert t.transition is not None and t.transition.to is FsmState.S1


@pytest.mark.asyncio
async def test_a_question_with_the_read_back_confirmed_resumes_in_the_state_it_moved_to(
    stored: dict[str, tuple[str, Any]],
) -> None:
    """The turn's own answer moves the session on (every value confirmed: the engine decides,
    S1.4); the answer leads, and the prompt after the bridge is S2's first question, put by S2's
    enter hook after the answer was composed."""
    stored.update({slot: ("confirmed", value) for slot, value in TDD.items()})
    nothing = retrieved([], {}).model_copy(
        update={"abstained": True, "abstain_reason": "NO_SUFFICIENT_EVIDENCE"}
    )
    t = s1_turn(
        "what is a waiting period?",
        models=Models(side_query="what is a waiting period?"),
        prompt="s1.readback",
    )
    t.retrieval = Evidence(nothing)

    await run(t)

    assert t.transition is not None
    assert (t.transition.row_id, t.transition.to) == ("S1.4", FsmState.S2)
    assert ids(t)[0] == "template:abstain"
    assert ids(t)[-3:] == [
        "template:side_query_bridge",
        "template:screening_done",
        "template:RL-S2-GOALS",
    ]


@pytest.mark.asyncio
async def test_an_under_18_age_in_a_compound_turn_drops_the_answer_for_the_erasure() -> None:
    said = "I'm 16, and how does 80C work?"
    models = Models(
        side_query="how does 80C work?",
        slots=({"slot": "age_years", "value": 16, "confidence": 0.95, "evidence_span": "16"},),
    )
    t = asked_s1(said, models=models)
    state = GraphState()
    await nodes.input_node(state, rt(t))
    await nodes.route(state, rt(t))
    await nodes.side_query(state, rt(t))
    assert t.render is not None  # answered, until the transition is known

    assert (await nodes.decide(state, rt(t))).goto == "data_erasure"

    assert t.transition is not None and t.transition.to is FsmState.DATA_ERASURE
    assert (t.render, t.draft, t.handles) == (None, None, None)  # the erasure speaks alone
    assert t.slot_rows == []


# --- limits and failures --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_full_stack_answers_briefly_and_returns_to_the_pending_question() -> None:
    t = asked_s1()
    t.settings = t.settings.model_copy(update={"side_query_max_stack": 0})

    await run(t)

    assert ids(t) == ["template:abstain", "template:side_query_bridge", "template:RL-S1-AGE"]
    assert t.retrieval.asked == []  # type: ignore[union-attr]
    assert t.side_outcome == "stack_full" and t.next is not None and t.next.stack == []


@pytest.mark.asyncio
async def test_retrieval_down_abstains_with_an_advisor_and_the_state_is_untouched() -> None:
    down = Down()
    t = asked_s1(
        "is the death benefit paid in instalments?", retrieval=down, pending_slot="age_years"
    )
    before = t.next.model_dump(exclude={"counters"})  # type: ignore[union-attr]

    await run(t)

    assert down.asked == 1
    assert ids(t)[:2] == ["template:abstain", "template:side_query_caveat"]
    assert ids(t)[-1] == "template:RL-S1-AGE"
    assert "HUMAN_REQUEST" in kinds(t) and t.side_outcome == "unavailable"
    assert t.next is not None and t.next.model_dump(exclude={"counters"}) == before
    assert t.transition is not None and t.transition.to is FsmState.S1


@pytest.mark.asyncio
async def test_five_side_queries_in_a_row_offer_to_carry_on_or_hand_off() -> None:
    t = asked_s1(counters={"side_queries": 4})

    await run(t)

    assert ids(t)[-1] == "template:side_query_offer"
    assert "template:side_query_bridge" not in ids(t)
    assert kinds(t) == ["CONTINUE", "HUMAN_REQUEST"]
    assert t.next is not None and t.next.counters["side_queries"] == 0

    fourth = asked_s1(counters={"side_queries": 3})
    await run(fourth)
    assert fourth.next is not None and fourth.next.counters["side_queries"] == 4
    assert ids(fourth)[-2:] == ["template:side_query_bridge", "template:RL-S1-AGE"]


@pytest.mark.asyncio
async def test_any_other_turn_ends_the_run_of_side_queries() -> None:
    t = s1_turn("34", prompt="s1.ask:age_years", counters={"side_queries": 3})

    await run(t)

    assert t.next is not None and t.next.counters["side_queries"] == 0


# --- S0: the approved privacy FAQ only ------------------------------------------------------------
@pytest.mark.asyncio
async def test_s0_answers_only_from_the_privacy_faq_and_generates_nothing() -> None:
    models = Models(intents=("GENERAL_FAQ",), side_query="what do you do with my data?")
    t = s0_turn(models, text="what do you do with my data?")
    t.retrieval = Evidence(tax_evidence())

    await run(t)

    faq = side_query.privacy_faq("en-IN", "dev")
    assert ids(t) == [f"faq:{faq.version}:PF-PURPOSES"]
    assert t.form is not None  # the consent form, still open
    assert t.released is not None and t.released.text == faq.entries[-1].answer
    assert not [r for r in models.routes if r.startswith("gen-")]
    assert t.retrieval.asked == []  # type: ignore[union-attr]
    assert t.side_outcome == "faq:PF-PURPOSES"


@pytest.mark.asyncio
async def test_s0_a_question_outside_the_faq_gets_the_s0_caveat() -> None:
    models = Models(intents=("GENERAL_FAQ",), side_query="how does 80C work?")
    t = s0_turn(models, text="how does 80C work?")

    await run(t)

    assert ids(t) == ["template:side_query_caveat"]
    assert t.released is not None and t.released.text == SCRIPTS.side_query_caveat["S0"]


def test_the_faq_matches_specific_entries_first() -> None:
    faq = side_query.privacy_faq("en-IN", "dev")

    assert faq.match("I want to complain about my data")
    assert faq.match("I want to complain about my data").id == "PF-COMPLAINT"  # type: ignore[union-attr]
    assert faq.match("how long do you keep my data?").id == "PF-RETENTION"  # type: ignore[union-attr]
    assert faq.match("why do you need this?").id == "PF-PURPOSES"  # type: ignore[union-attr]
    assert faq.match("what is a rider?") is None


def test_the_faq_loader_refuses_a_dummy_faq_in_pilot_and_a_broken_file(tmp_path: Path) -> None:
    with pytest.raises(side_query.FaqError, match="DUMMY_REFUSED"):
        side_query.privacy_faq("en-IN", "pilot")
    (tmp_path / "privacy-en-IN.yaml").write_text("version: x\n", encoding="utf-8")
    with pytest.raises(side_query.FaqError, match="INVALID"):
        side_query.privacy_faq("en-IN", "dev", tmp_path)
    with pytest.raises(side_query.FaqError, match="NOT_FOUND"):
        side_query.privacy_faq("hi-IN", "dev", tmp_path)


# --- tax ------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tax_asks_the_regime_when_unknown_and_not_once_it_is_confirmed(
    stored: dict[str, tuple[str, Any]],
) -> None:
    t = asked_s1("does 80C apply to this tax benefit?")
    await run(t)

    assert ids(t)[:6] == [
        "generated:answer",
        "template:side_query_caveat",
        "template:tax_condition",
        "template:regime_ask",
        "registry:DISC-GLOBAL-TAX-05",
        "template:sources",
    ]
    assert kinds(t)[:2] == ["FACT", "FACT"]
    [(_, ctx)] = t.retrieval.asked  # type: ignore[union-attr]
    assert (ctx.regime, ctx.tax_year, ctx.fsm_state) == (None, "2026-27", "S1")

    stored["tax_regime"] = ("confirmed", "old")
    known = asked_s1("does 80C apply to this tax benefit?")
    await run(known)
    assert "template:regime_ask" not in ids(known)
    [(_, ctx)] = known.retrieval.asked  # type: ignore[union-attr]
    assert ctx.regime == "old"


@pytest.mark.parametrize(
    ("question", "now", "year"),
    [
        ("how does 80C work?", NOW, "2026-27"),
        ("how did 80C work in FY 2025-26?", NOW, "2025-26"),
        ("and for AY 2026-27?", NOW, "2025-26"),
        ("how does 80C work?", datetime(2026, 3, 1, tzinfo=UTC), "2025-26"),
        ("what about 2025-27?", NOW, "2026-27"),  # not a tax year: the current one
    ],
)
def test_the_tax_year_is_the_one_named_else_the_current_one(
    question: str, now: datetime, year: str
) -> None:
    assert side_query.tax_year(question, now) == year


@pytest.mark.asyncio
async def test_a_regime_revealed_in_a_question_is_proposed_then_confirmed(
    stored: dict[str, tuple[str, Any]],
) -> None:
    said = "I'm on the old regime, does 80C apply?"
    t = s2_turn(said, models=Models(side_query=said, recommend=(CITED_TAX,)), prompt="s2.ask:goals")
    t.retrieval = Evidence(tax_evidence())
    await run(t)

    assert [(r.slot, r.value, r.status) for r in t.slot_rows] == [("tax_regime", "old", "proposed")]
    assert "template:regime_confirm" in ids(t) and "template:regime_ask" not in ids(t)
    [(_, ctx)] = t.retrieval.asked  # type: ignore[union-attr]
    assert ctx.regime is None  # never used as said
    assert kinds(t)[:2] == ["FACT", "FACT"]
    confirm = t.quick_replies[0]["action"]

    yes = s2_turn(action=confirm, prompt="s2.ask:goals")
    await run(yes)
    assert [(r.slot, r.value, r.status) for r in yes.slot_rows] == [
        ("tax_regime", "old", "confirmed")
    ]
    assert ids(yes)[:2] == ["template:regime_noted", "template:RL-S2-GOALS"]

    pre_consent = s0_turn(action=confirm)
    await run(pre_consent)
    assert pre_consent.slot_rows == []  # I1: nothing before a valid P1


# --- the header and the logs ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_release_header_names_the_outcome_and_the_language(
    monkeypatch: pytest.MonkeyPatch, audited: Recorder
) -> None:
    StoreCalls(monkeypatch)
    t = asked_s1()
    await run(t)
    await nodes.commit(GraphState(), rt(t))

    [header] = [e["header"] for e in audited.events if e["event_type"].value == "RESPONSE_RELEASED"]
    assert (header.faq, header.language, header.objection) == ("answered", "en", None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
async def test_no_question_or_answer_reaches_the_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from surakshasetu.logging import configure_logging

    configure_logging("DEBUG", tmp_path)
    said = "does 80C cover Sentinel Ridge Road and the old regime?"
    t = asked_s1(said, models=Models(side_query=said, recommend=(CITED_TAX,)))
    await run(t)

    logged = capsys.readouterr().out + files(tmp_path)
    assert "side query answered from 1 evidence chunks" in logged
    for leaked in ("Sentinel Ridge", "old regime", "Premiums qualify"):
        assert leaked not in logged
