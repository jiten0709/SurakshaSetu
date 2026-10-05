"""Quote-Only (Step 19): the plan named by the customer (never proposed, I3), age and tobacco read
back, the quote at the product's quote_defaults or a stated cover, the indicative card with
DISC-GLOBAL-QUOTE-02 verbatim, the quote adapter's problems, the declined-tobacco path, and the
rows: the hard block (C13), satisfied -> Exit, and the opt-in to S2 or S1. The domain tier is
runtime_support's stand-in."""

import logging
from typing import Any

import pytest
from runtime_support import ROP, SAVER, TERM, Models
from test_runtime_nodes import Recorder, run
from test_s0 import ids, transition, valid_record
from test_s1 import Tier, candidate, s1_turn, stored, until_decide

from surakshasetu.domain.models import EligibilityResult
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph.nodes import Turn
from surakshasetu.graph.state import EligibilityPayload
from surakshasetu.graph.states import quote_only

QUOTES = "/v1/quotes"
__all__ = ["stored"]  # test_s1's autouse fixture: no stored slot rows, no cached catalog names
CARD = (
    "Indicative premium for Suraksha Term Shield (UIN 999N001V02): ₹16,000 a year.\n"
    "Cover: ₹1,00,00,000 for 30 years. Premium payment: Regular pay. Valid until 3 November 2026."
)


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


def qo_turn(
    text: str | None = None,
    *,
    focus: tuple[str, ...] = (),
    **kwargs: Any,
) -> Turn:
    t = s1_turn(text, state=FsmState.QUOTE_ONLY, focus_uins=list(focus), **kwargs)
    return t


def known(stored: dict[str, tuple[str, Any]], status: str = "confirmed", **values: Any) -> None:
    for slot, value in ({"age_years": 34, "tobacco_12m": False} | values).items():
        stored[slot] = (status, value)


def eligible() -> EligibilityPayload:
    result = EligibilityResult.model_validate(
        {
            "decision_id": "0199a1b2-0000-7000-8000-00000000e11e",
            "outcome": "ELIGIBLE",
            "eligible_uins": [TERM, ROP],
            "uw_path": "standard",
            "flags": [],
            "rule_ids": ["E-01"],
            "reason_codes": [],
            "rules_version": "2026.09.1",
            "params_version": "actuarial-dummy-2026.09.1",
            "inputs_sha256": "a" * 64,
        }
    )
    return EligibilityPayload(
        age_years=34,
        residency="resident",
        pincode="411001",
        tobacco_12m=False,
        occupation_class="OCC-OFFICE-01",
        health_flags={},
        engine=result,
    )


# --- the plan -------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_without_a_named_plan_it_asks_for_one_and_names_none() -> None:
    t = qo_turn("I want a price")
    await run(t)
    assert ids(t) == ["template:plan_ask"] and t.released is not None
    assert "Suraksha" not in t.released.text and "999N" not in t.released.text
    assert t.next is not None and t.next.last_prompt_id == quote_only.PLAN


@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["the Suraksha Term Shield please", "999N001V02"])
async def test_a_plan_named_by_name_or_uin_is_the_focus_and_age_is_asked(said: str) -> None:
    t = qo_turn(said, prompt=quote_only.PLAN)
    await run(t)
    assert t.next is not None and t.next.focus_uins == [TERM]
    assert ids(t) == ["template:RL-S1-AGE"]  # template only: Quote-Only generates nothing


@pytest.mark.asyncio
async def test_the_longest_name_wins() -> None:
    t = qo_turn("price the Suraksha Term Shield ROP", prompt=quote_only.PLAN)
    await run(t)
    assert t.next is not None and t.next.focus_uins == [ROP]


# --- the quote ------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_read_back_then_the_quote_at_the_products_defaults_with_quote_02(
    stored: dict[str, tuple[str, Any]], audited: Recorder
) -> None:
    known(stored, "proposed")
    tier = Tier()
    t = qo_turn("yes", tier=tier, focus=(TERM,), prompt="qo.readback")

    await run(t)

    assert {r.status for r in t.slot_rows} == {"confirmed"}
    (request,) = tier.bodies(QUOTES)
    assert {
        k: request[k] for k in ("uin", "sum_assured_inr", "term_years", "ppt", "frequency")
    } == {
        "uin": TERM,
        "sum_assured_inr": "10000000",
        "term_years": 30,
        "ppt": "regular",
        "frequency": "annual",
    }
    assert (request["age_years"], request["tobacco_12m"], request["rider_uins"]) == (34, False, [])
    assert ids(t) == [
        "template:quote_card",
        "registry:DISC-GLOBAL-QUOTE-02",
        "template:quote_caveat",
        "template:quote_next",
    ]
    assert t.released is not None and t.released.text.startswith(CARD)
    assert "DUMMY: Premium is indicative, final after underwriting." in t.released.text
    decision = next(e for e in audited.events if e["event_type"].value == "ENGINE_DECISION")
    assert decision["header"].service == "quote"
    assert t.next is not None and t.next.quote is not None
    assert [r["action"]["type"] for r in t.quick_replies] == ["OPT_IN", "SATISFIED"]
    assert transition(t) == ("QO.STAY", FsmState.QUOTE_ONLY)


@pytest.mark.asyncio
async def test_age_and_tobacco_collected_here_are_read_back_before_any_quote(
    stored: dict[str, tuple[str, Any]],
) -> None:
    stored["age_years"] = ("proposed", 34)
    tier = Tier()
    t = qo_turn("no", tier=tier, focus=(TERM,), prompt="qo.ask:tobacco_12m")
    await run(t)
    assert ids(t) == ["template:readback"] and tier.bodies(QUOTES) == []
    assert t.released is not None
    assert t.released.text == "To confirm: 34, no tobacco in the last 12 months. Correct?"


@pytest.mark.asyncio
async def test_a_cover_off_the_step_is_re_asked_with_the_problems_bounds(
    stored: dict[str, tuple[str, Any]],
) -> None:
    known(stored)
    models = Models(slots=(candidate("sum_assured_inr", None, "1.23 crore"),))
    t = qo_turn("what about 1.23 crore", models=models, focus=(TERM,), prompt=quote_only.QUOTED)

    await run(t)

    assert ids(t) == ["template:cover_bounds"] and t.released is not None
    assert t.released.text == (
        "For Suraksha Term Shield, cover can be from ₹25,00,000 to ₹10,00,00,000, in steps of"
        " ₹5,00,000. How much cover would you like?"
    )
    assert [(r.slot, r.value, r.status) for r in t.slot_rows][-1] == (
        "sum_assured_inr",
        None,
        "declined",
    )
    assert t.next is not None and t.next.last_prompt_id == "qo.ask:sum_assured_inr"

    stored["sum_assured_inr"] = ("declined", None)
    tier = Tier()
    again = qo_turn("1.25 crore", tier=tier, focus=(TERM,), prompt="qo.ask:sum_assured_inr")
    await run(again)
    assert tier.bodies(QUOTES)[0]["sum_assured_inr"] == "12500000"
    assert ids(again)[0] == "template:quote_card"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("focus", "age", "part"),
    [(SAVER, 34, "plan_unavailable"), ("999N099V01", 34, "plan_unknown"), (ROP, 60, "age_bounds")],
)
async def test_a_plan_that_cannot_be_priced_asks_for_another(
    stored: dict[str, tuple[str, Any]], focus: str, age: int, part: str
) -> None:
    known(stored, age_years=age)
    t = qo_turn(action={"type": "RETRY", "payload": {}}, focus=(focus,))
    await run(t)
    assert ids(t) == [f"template:{part}"]
    assert t.next is not None and t.next.focus_uins == []
    assert t.next.last_prompt_id == quote_only.PLAN


@pytest.mark.asyncio
async def test_a_declined_tobacco_answer_gets_no_premium_and_no_quote_call(
    stored: dict[str, tuple[str, Any]],
) -> None:
    known(stored, tobacco_12m=None)
    tier = Tier()
    t = qo_turn(action={"type": "RETRY", "payload": {}}, tier=tier, focus=(TERM,))
    await run(t)
    assert ids(t) == ["template:quote_withheld"] and tier.bodies(QUOTES) == []


@pytest.mark.asyncio
async def test_the_rating_engine_down_gives_the_retry_and_no_premium(
    stored: dict[str, tuple[str, Any]],
) -> None:
    known(stored)
    t = qo_turn(action={"type": "RETRY", "payload": {}}, tier=Tier(down=(QUOTES,)), focus=(TERM,))
    await run(t)
    assert ids(t) == ["template:rating_unavailable"]
    assert [r["action"]["type"] for r in t.quick_replies] == ["RETRY"]


# --- the rows -------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("said", ["apply now", "I want to buy this plan"])
async def test_a_request_to_apply_is_hard_blocked_and_the_state_unchanged(said: str) -> None:
    t = qo_turn(said, focus=(TERM,), prompt=quote_only.QUOTED)
    await run(t)
    assert transition(t) == ("QO.1", FsmState.QUOTE_ONLY)
    assert t.transition is not None and t.transition.reason_code == "HARD_BLOCK_C13"
    assert ids(t) == ["template:hard_block"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("purposes", "parts"), [(("P1",), 1), (("P1", "P3"), 2)])
async def test_satisfied_exits_with_the_summary_and_reengagement_only_with_p3(
    stored: dict[str, tuple[str, Any]], purposes: tuple[str, ...], parts: int
) -> None:
    known(stored)
    shown = qo_turn("yes", focus=(TERM,), prompt="qo.readback")
    await run(shown)
    assert shown.next is not None

    t = qo_turn(
        "no",
        focus=(TERM,),
        prompt=quote_only.QUOTED,
        quote=shown.next.quote,
        consent=valid_record(*purposes),
    )
    await run(t)
    assert transition(t) == ("QO.2", FsmState.EXIT)
    assert ids(t) == ["template:quote_summary", "template:reengage"][:parts]
    assert t.released is not None and "₹16,000" in t.released.text


@pytest.mark.asyncio
async def test_an_opt_in_goes_to_s2_when_eligible_and_to_s1_otherwise(
    stored: dict[str, tuple[str, Any]],
) -> None:
    t = qo_turn(action={"type": "OPT_IN", "payload": {}}, eligibility=eligible())
    await run(t)
    assert transition(t) == ("QO.3", FsmState.S2)
    assert ids(t) == ["template:screening_done", "template:RL-S2-GOALS"]  # Step 20: S2 asks

    known(stored)  # age and tobacco from Quote-Only carry over to S1
    s0_entry = qo_turn("yes", focus=(TERM,), prompt=quote_only.QUOTED)
    await run(s0_entry)
    assert transition(s0_entry) == ("QO.3b", FsmState.S1)
    assert ids(s0_entry)[-1] == "template:RL-S1-RESIDENCY"


@pytest.mark.asyncio
async def test_without_valid_consent_quote_only_calls_nothing() -> None:
    tier = Tier()
    t = qo_turn("Suraksha Term Shield", tier=tier, prompt=quote_only.PLAN)
    assert t.next is not None
    t.next.consent = None
    await until_decide(t)
    assert tier.calls == [] and transition(t) == ("G1", FsmState.S0)


@pytest.mark.asyncio
async def test_no_customer_value_reaches_the_logs(
    stored: dict[str, tuple[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    known(stored, age_years=47)
    caplog.set_level(logging.DEBUG, logger="surakshasetu")
    await run(qo_turn("yes", focus=(TERM,), prompt="qo.readback"))
    assert "47" not in caplog.text and "16,000" not in caplog.text and "16000" not in caplog.text
