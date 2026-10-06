"""The objection handler (Step 22, TDD §3.9's CC5): what counts as an objection, each type's answer
(the engine's alternatives with gaps; the claim process, cited; no comparison; no promise, then the
wording; an acknowledgment), the second time on the same point (Pause or Exit offered, no rebuttal),
a deferral (CC3b Pause outside S3, S3.2 inside), END (CC5b Exit), and the RESPONSE_RELEASED header.
S3's fixtures (the domain stand-in, the store, the clock) are reused."""

from typing import Any

import pytest
from compose_support import chunk, retrieved
from runtime_support import Models
from test_runtime_nodes import BUNDLE, Recorder, StoreCalls, rt, run
from test_s0 import valid_record
from test_s1 import s1_turn
from test_s3 import (  # noqa: F401  (the fixtures are used by name)
    ALTERNATIVES,
    Evidence,
    Tier,
    audited,
    events,
    follow,
    ids,
    journey,
    kinds,
    presented,
    stored,
)

from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import nodes
from surakshasetu.graph.handlers import objection
from surakshasetu.graph.state import GraphState
from surakshasetu.store import conv as store

SCRIPTS = BUNDLE.templates["en-IN"].scripts
DIGIT = set("0123456789")


def detected(text: str | None, state: FsmState, intents: tuple[str, ...] = (), **kw: Any) -> Any:
    return s1_turn(text, models=Models(intents=intents), state=state, **kw)


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, Any]]:
    """S1 with nothing answered yet (S3's fixture holds every slot confirmed)."""
    rows: dict[str, tuple[str, Any]] = {}
    monkeypatch.setattr(store, "latest_slots", lambda *a: dict(rows))
    return rows


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "intents", "state", "kind"),
    [
        ("this is too expensive", ("OBJECTION_PRICE",), FsmState.S3, "price"),
        ("insurers never pay claims", ("OBJECTION_TRUST",), FsmState.S2, "trust"),
        ("X insurer is cheaper", ("OBJECTION_COMPETITOR",), FsmState.S1, "competitor"),
        ("insurance is a waste", ("OBJECTION_OTHER",), FsmState.S2, "other"),
        ("I want guaranteed high returns", (), FsmState.S2, "guarantee"),  # the needs lexicon
        ("is the LIC plan better?", (), FsmState.S2, "competitor"),
        ("can you make it cheaper", (), FsmState.S3, "price"),  # the s3 lexicon
        ("I need some time to think", ("NEED_TIME",), FsmState.S2, "deferral"),
        ("I need some time to think", ("NEED_TIME",), FsmState.S3, None),  # S3.2 decides
        ("this is too expensive", ("OBJECTION_PRICE",), FsmState.S0, None),  # S0: privacy only
        ("34", (), FsmState.S1, None),
    ],
)
async def test_what_counts_as_an_objection(
    text: str, intents: tuple[str, ...], state: FsmState, kind: str | None
) -> None:
    t = detected(text, state, intents)
    await nodes.input_node(GraphState(), rt(t))
    assert objection.detect(t) == kind


def test_the_end_and_save_actions() -> None:
    end = s1_turn(action={"type": "END", "payload": {}}, state=FsmState.S3)
    save_s2 = s1_turn(action={"type": "SAVE", "payload": {}}, state=FsmState.S2)
    save_s3 = s1_turn(action={"type": "SAVE", "payload": {}}, state=FsmState.S3)
    assert (objection.detect(end), objection.detect(save_s2), objection.detect(save_s3)) == (
        "end",
        "deferral",
        None,
    )


# --- each type, the first time --------------------------------------------------------------------
@pytest.mark.asyncio
async def test_price_in_s3_offers_the_engines_alternatives_with_their_gaps(
    monkeypatch: pytest.MonkeyPatch,
    audited: Recorder,  # noqa: F811
) -> None:
    held = await presented(monkeypatch)
    tier = Tier()
    t = await follow(
        held, "this is too expensive", models=Models(intents=("OBJECTION_PRICE",)), tier=tier
    )

    assert ids(t)[0] == "template:alternatives"
    assert tier.bodies(ALTERNATIVES), "the alternatives come from the engine"
    assert [e["header"].service for e in events(audited, "ENGINE_DECISION")][-1] == "alternatives"
    assert t.objection == "price" and t.objection_response == "alternatives"
    assert t.transition is not None and t.transition.row_id == "CC5"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh")
async def test_price_before_any_premium_says_when_and_gives_no_figure() -> None:
    t = s1_turn(
        "this is too expensive",
        models=Models(intents=("OBJECTION_PRICE",)),
        prompt="s1.ask:age_years",
    )
    await run(t)

    assert ids(t)[0] == "template:objection_price_early"
    assert t.released is not None and not DIGIT & set(SCRIPTS.objection_price_early)
    assert t.objection_response == "no_figure"


@pytest.mark.asyncio
async def test_trust_answers_the_claim_process_from_the_corpus_cited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    claims = retrieved(
        [chunk("E1", domain="regulatory", text="DUMMY: A death claim is settled within 30 days.")],
        {"regulatory": 1},
    )
    evidence = Evidence(claims)
    models = Models(
        intents=("OBJECTION_TRUST",),
        recommend=("A death claim is settled within the time the rules set [E1].",),
    )
    t = await follow(held, "insurers never pay claims", models=models, retrieval=evidence)

    assert ids(t)[0] == "generated:answer" and t.released is not None
    assert t.released.rendered is not None and t.released.rendered.citations
    [(_, ctx)] = evidence.asked
    assert "claims" in ctx.entities
    assert t.objection_response == "cited:answered"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh")
async def test_a_competitor_is_not_compared() -> None:
    t = s1_turn(
        "X insurer is cheaper",
        models=Models(intents=("OBJECTION_COMPETITOR",)),
        prompt="s1.ask:age_years",
    )
    await run(t)

    assert ids(t)[:2] == ["template:competitor_note", "template:RL-S1-AGE"]
    assert t.released is not None and BUNDLE.manifest.insurer in t.released.text


@pytest.mark.asyncio
async def test_a_guarantee_gets_no_promise_then_the_wording_once_plans_are_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, Any] = {}
    real = store.latest_slots
    monkeypatch.setattr(store, "latest_slots", lambda *a: dict(rows))
    early = s1_turn(
        "is it guaranteed?",
        models=Models(intents=("OBJECTION_GUARANTEE",)),
        prompt="s1.ask:age_years",
    )
    await run(early)
    assert ids(early)[:2] == ["template:guarantee_note", "template:RL-S1-AGE"]
    assert early.objection_response == "no_promise"

    monkeypatch.setattr(store, "latest_slots", real)
    held = await presented(monkeypatch)
    wording = Evidence(retrieved([chunk("E1")], {"product": 1}))
    models = Models(
        intents=("OBJECTION_GUARANTEE",),
        recommend=("The policy wording defines the death benefit [E1].",),
    )
    t = await follow(held, "can you guarantee my returns", models=models, retrieval=wording)
    assert ids(t)[:2] == ["template:guarantee_note", "generated:answer"]
    assert ids(t)[-1] == "template:s3_choices"  # back to the four choices
    assert t.objection_response == "wording:answered"


@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh")
async def test_anything_else_is_acknowledged_and_the_decision_stays_theirs() -> None:
    t = s1_turn(
        "insurance is a waste",
        models=Models(intents=("OBJECTION_OTHER",)),
        prompt="s1.ask:age_years",
    )
    await run(t)

    assert ids(t)[:2] == ["template:objection_other", "template:RL-S1-AGE"]


# --- the second time on the same point ------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh")
async def test_the_same_objection_again_offers_pause_or_exit_and_no_rebuttal() -> None:
    second = s1_turn(
        "still too expensive",
        models=Models(intents=("OBJECTION_PRICE",)),
        prompt="s1.ask:age_years",
        counters={"objection_price": 1},
    )
    await run(second)

    assert ids(second) == ["template:objection_repeat"]
    assert kinds(second) == ["SAVE", "END", "CONTINUE"]
    assert second.objection_response == "offer_pause_exit"
    assert second.next is not None and second.next.counters["objection_price"] == 2

    other = s1_turn(
        "X insurer is cheaper",
        models=Models(intents=("OBJECTION_COMPETITOR",)),
        prompt="s1.ask:age_years",
        counters={"objection_price": 1},
    )
    await run(other)
    assert ids(other)[0] == "template:competitor_note"  # another point: answered


@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh")
async def test_save_after_the_offer_pauses_and_end_exits() -> None:
    save = s1_turn(action={"type": "SAVE", "payload": {}}, prompt="s1.ask:age_years")
    await run(save)
    assert save.transition is not None and save.transition.row_id == "CC3b"
    assert save.transition.to is FsmState.PAUSE
    assert ids(save) == ["template:paused"]
    assert save.next is not None and save.next.stack[-1].state is FsmState.S1

    end = s1_turn(
        action={"type": "END", "payload": {}},
        prompt="s1.ask:age_years",
        consent=valid_record("P1", "P3"),
    )
    await run(end)
    assert end.transition is not None
    assert (end.transition.row_id, end.transition.to) == ("CC5b", FsmState.EXIT)
    assert ids(end) == ["template:declined_exit"]  # no quote to be reminded of before S3


@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh")
async def test_a_deferral_outside_s3_pauses_even_when_read_as_an_objection() -> None:
    t = s1_turn(
        "let me discuss with my wife",
        models=Models(intents=("NEED_TIME", "OBJECTION_OTHER")),
        prompt="s1.ask:age_years",
    )
    await run(t)

    assert t.transition is not None and t.transition.row_id == "CC3b"
    assert ids(t) == ["template:paused"]


# --- the audit header -----------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.usefixtures("fresh")
async def test_the_release_header_records_the_objection_and_the_answer(
    monkeypatch: pytest.MonkeyPatch,
    audited: Recorder,  # noqa: F811
) -> None:
    t = s1_turn(
        "X insurer is cheaper",
        models=Models(intents=("OBJECTION_COMPETITOR",)),
        prompt="s1.ask:age_years",
    )
    await run(t)
    StoreCalls(monkeypatch)
    await nodes.commit(GraphState(), rt(t))

    [header] = [e["header"] for e in events(audited, "RESPONSE_RELEASED")]
    assert (header.objection, header.objection_response, header.faq) == (
        "competitor",
        "declined",
        None,
    )


@pytest.mark.asyncio
async def test_an_objection_never_holds_back_a_move_the_turns_own_answer_earned() -> None:
    """The read-back confirmed in the same turn as an objection: S1.4 still moves to S2 (TDD §2.6:
    answer, then resume where the answer put the customer), and the answer leads the reply."""
    t = s1_turn(
        "yes, but is it guaranteed?",
        models=Models(intents=("OBJECTION_GUARANTEE",)),
        prompt="s1.readback",
    )
    await run(t)

    assert t.transition is not None
    assert (t.transition.row_id, t.transition.to) == ("S1.4", FsmState.S2)
    assert ids(t)[0] == "template:guarantee_note"
