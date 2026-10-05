"""State-2 (Step 20): the rules' slots in order with their reasons and one empathetic sentence, each
kind of answer, money read back, the summary with the engine's assumptions, the needs hash bound on
confirmation (I2), S2's rows (XA, HE, S3, the election, amber), the edge cases, the dependency-down
pause, hydration, I1, and the V4 needs correction from S3. The domain tier is runtime_support's
MockTransport stand-in; the store's slot reads are patched; audit appends are recorded."""

import json
import logging
from typing import Any

import httpx
import pytest
from runtime_support import FRIENDLY, Models, domain, domain_handler, suitability_json
from test_runtime_nodes import BUNDLE, Recorder, rt, run, session, turn
from test_s0 import ids, transition, valid_record
from test_s1 import TDD, candidate, rows

from surakshasetu.domain.models import EligibilityResult, NeedsPayload
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import handlers, nodes
from surakshasetu.graph.nodes import Turn
from surakshasetu.graph.state import EligibilityPayload, GraphState, SessionState
from surakshasetu.graph.states import s2
from surakshasetu.store import conv as store

SCRIPTS = BUNDLE.templates["en-IN"].scripts
SLOTS = BUNDLE.templates["en-IN"].slots
EVALUATE = "/v1/suitability/evaluate"
# The TDD §3.7 example's needs, as S2 stores them (content/testvectors/needs/01-tdd-example.json).
NEEDS = {
    "goals": ["income_protection", "loan_cover"],
    "annual_income_inr": "2400000",
    "income_type": "salaried",
    "dependants": [{"relation": "spouse", "age": 32}, {"relation": "child", "age": 4}],
    "liabilities": [{"kind": "home", "outstanding_inr": "3500000", "years_left": 18}],
    "existing_cover_inr": "0",
    "employer_cover_inr": "1500000",
    "existing_annual_premium_inr": "0",
    "earmarked_assets_inr": None,
    "premium_budget_inr_pa": "30000",
}
SUMMARY = """Here is what you told me, and the assumptions used:
Goals: Protect your family's income, Repay loans
Annual income: ₹24,00,000 a year
Income type: Salaried
Dependants: spouse (32), child (4)
Loans: home loan, ₹35,00,000 outstanding, 18 years left
Your own life cover: none
Employer group cover: ₹15,00,000
Premiums on existing policies: none
Money set aside: not shared
Premium budget: ₹30,000 a year
Existing cover counted: ₹7,50,000
Assumptions: cover to age 60, 26 years of dependency, income growth of 5% a year, a discount rate \
of 7% a year, 30% of income as your own spending, and ₹2,00,000 for final expenses."""


class Tier:
    """runtime_support's domain stand-in, recording every call. `suit` overrides the Suitability
    Service's result; `down` lists path prefixes that are unreachable."""

    def __init__(self, *, down: tuple[str, ...] = (), **suit: Any) -> None:
        self.down, self.suit = down, suit
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        if any(request.url.path.startswith(d) for d in self.down):
            raise httpx.ConnectError("down")
        if request.url.path == EVALUATE and self.suit:
            return httpx.Response(200, json=suitability_json(body, **self.suit))
        return domain_handler(request)

    def bodies(self, path: str) -> list[Any]:
        return [b for p, b in self.calls if p == path]


def eligible() -> EligibilityPayload:
    request = {
        "age_years": 34,
        "residency": "resident",
        "pincode": "411001",
        "tobacco_12m": False,
        "occupation_code": "OCC-OFFICE-01",
        "health_flags": {"RL-S1-HEALTH": False},
        "proposer": {"is_life_assured": True},
    }
    return EligibilityPayload(
        age_years=34,
        residency="resident",
        pincode="411001",
        tobacco_12m=False,
        occupation_class="OCC-OFFICE-01",
        health_flags={"RL-S1-HEALTH": False},
        engine=EligibilityResult.model_validate(eligibility(request)),
    )


def eligibility(request: dict[str, Any]) -> dict[str, Any]:
    from runtime_support import eligibility_json

    return eligibility_json(request)


def s2_turn(
    text: str | None = None,
    *,
    action: dict[str, Any] | None = None,
    models: Models | None = None,
    tier: Tier | None = None,
    prompt: str | None = None,
    state: FsmState = FsmState.S2,
    **update: Any,
) -> Turn:
    t = turn(models, text=text)
    t.action = action
    t.domain = domain(tier or Tier())
    base = {"fsm_state": state, "consent": valid_record(), "eligibility": eligible()}
    t.next = session(**(base | {"last_prompt_id": prompt} | update))
    t.row = t.row.__class__(**{**t.row.__dict__, "fsm_state": state.value})
    t.from_state = state
    return t


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


@pytest.fixture(autouse=True)
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, Any]]:
    """conv.slot_value's newest row per slot: S1's confirmed answers, plus what a test adds."""
    found: dict[str, tuple[str, Any]] = {slot: ("confirmed", v) for slot, v in TDD.items()}
    monkeypatch.setattr(store, "latest_slots", lambda *a: dict(found))
    monkeypatch.setattr(handlers, "_PRODUCT_NAMES", {})
    return found


def answered(stored: dict[str, tuple[str, Any]], status: str = "proposed", **values: Any) -> None:
    for slot, value in (NEEDS | values).items():
        stored[slot] = ("declined" if value is None and status != "confirmed" else status, value)


def keep(stored: dict[str, tuple[str, Any]], t: Turn) -> None:
    """The turn's slot rows, as the next turn reads them back."""
    for r in t.slot_rows:
        stored[r.slot] = (r.status, r.value)


def events(audited: Recorder, kind: str) -> list[dict[str, Any]]:
    return [e for e in audited.events if e["event_type"].value == kind]


async def summarised(
    stored: dict[str, tuple[str, Any]], tier: Tier | None = None, **values: Any
) -> SessionState:
    """Every slot answered and the last one read back: the summary turn. Its session holds the
    unbound needs and the suitability record."""
    answered(stored, **values)
    t = s2_turn("yes", tier=tier, prompt="s2.confirm:premium_budget_inr_pa")
    await run(t)
    assert t.next is not None and t.next.last_prompt_id == "s2.readback", ids(t)
    keep(stored, t)
    return t.next


async def confirm(
    stored: dict[str, tuple[str, Any]], held: SessionState, text: str = "yes", **kw: Any
) -> Turn:
    """The next turn on the session the last one left, through the checkpoint's JSON round trip
    (which keeps values, not which members were set)."""
    held = SessionState.model_validate(held.model_dump(mode="json"))
    t = s2_turn(
        text,
        prompt=held.last_prompt_id,
        needs=held.needs,
        suitability=held.suitability,
        counters=held.counters,
        **kw,
    )
    await run(t)
    keep(stored, t)
    return t


# --- the questions --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_first_question_is_goals_with_its_reason_and_one_empathetic_sentence() -> None:
    models = Models()
    t = s2_turn("hello", models=models)

    await run(t)

    goals = SLOTS["RL-S2-GOALS"]
    assert ids(t) == ["generated:narrative", "template:RL-S2-GOALS"]
    assert t.released is not None
    assert t.released.text == f"{FRIENDLY}\n\n{goals.question} {goals.reason}"
    assert t.next is not None and t.next.last_prompt_id == "s2.ask:goals"
    assert [r["label"] for r in t.quick_replies] == list(SCRIPTS.labels.goals.values())
    assert "gen-converse" in models.routes
    assert transition(t) == ("S2.STAY", FsmState.S2)


@pytest.mark.asyncio
async def test_entering_s2_appends_the_first_question_to_the_bridge() -> None:
    t = s2_turn()
    t.parts = [("screening_done", SCRIPTS.screening_done)]

    await s2.enter(GraphState(), runtime=rt(t))

    assert [i for i, _ in t.parts] == ["screening_done", "RL-S2-GOALS"]
    assert t.phrase is None  # not a plain question: no generated sentence


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answered_slots", "asked"),
    [
        (1, "RL-S2-INCOME"),
        (2, "RL-S2-INCOME-TYPE"),
        (3, "RL-S2-DEPENDANTS"),
        (4, "RL-S2-LIABILITIES"),
        (5, "RL-S2-EXISTING-COVER"),
        (6, "RL-S2-EMPLOYER-COVER"),
        (7, "RL-S2-EXISTING-PREMIUM"),
        (8, "RL-S2-ASSETS"),
        (9, "RL-S2-BUDGET"),
    ],
)
async def test_questions_follow_the_rules_order(
    stored: dict[str, tuple[str, Any]], answered_slots: int, asked: str
) -> None:
    for slot in list(NEEDS)[:answered_slots]:
        stored[slot] = ("proposed", NEEDS[slot])
    t = s2_turn("hello")
    await run(t)
    assert ids(t)[-1] == f"template:{asked}"


# --- each kind of answer --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_goals_are_ranked_in_the_order_the_customer_said_them() -> None:
    t = s2_turn("paying off the loan, and my family's income", prompt="s2.ask:goals")
    await run(t)
    assert rows(t) == {"goals": (["loan_cover", "income_protection"], "proposed")}
    assert ids(t)[-1] == "template:RL-S2-INCOME"


@pytest.mark.asyncio
async def test_a_goals_quick_reply_may_send_a_ranked_list() -> None:
    action = {"type": "SLOT", "payload": {"slot": "goals", "value": ["retirement", "savings"]}}
    t = s2_turn(action=action, prompt="s2.ask:goals")
    await run(t)
    assert rows(t) == {"goals": (["retirement", "savings"], "proposed")}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said", "value"),
    [
        ("I run my own business", "self_employed"),
        ("salaried", "salaried"),
        ("housewife", "homemaker"),
    ],
)
async def test_income_type_is_read_from_its_words(
    stored: dict[str, tuple[str, Any]], said: str, value: str
) -> None:
    stored["goals"], stored["annual_income_inr"] = ("proposed", ["loan_cover"]), ("proposed", "1")
    t = s2_turn(said, prompt="s2.ask:income_type")
    await run(t)
    assert rows(t)["income_type"] == (value, "proposed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said", "value"),
    [
        ("my wife is 32 and my son is 4", NEEDS["dependants"]),
        ("patni 30, beti 2", [{"relation": "spouse", "age": 30}, {"relation": "child", "age": 2}]),
        ("no one", []),
    ],
)
async def test_dependants_are_relations_and_ages_only(said: str, value: Any) -> None:
    t = s2_turn(said, prompt="s2.ask:dependants")
    await run(t)
    assert rows(t) == {"dependants": (value, "proposed")}


@pytest.mark.asyncio
async def test_a_dependant_without_an_age_gets_the_format_hint() -> None:
    t = s2_turn("my wife and my son", prompt="s2.ask:dependants")
    await run(t)
    assert rows(t) == {}
    assert ids(t) == ["template:format_hint", "template:RL-S2-DEPENDANTS"]
    assert SLOTS["RL-S2-DEPENDANTS"].hint in (t.released.text if t.released else "")


@pytest.mark.asyncio
async def test_a_loan_is_read_and_read_back_with_its_amount() -> None:
    t = s2_turn("home loan of 35 lakh, 18 years left", prompt="s2.ask:liabilities")
    await run(t)
    assert rows(t) == {"liabilities": (NEEDS["liabilities"], "proposed")}
    assert t.released is not None
    assert t.released.text == (
        "To confirm: home loan, ₹35,00,000 outstanding, 18 years left. Correct?"
    )
    assert t.next is not None and t.next.last_prompt_id == "s2.confirm:liabilities"


@pytest.mark.asyncio
async def test_loans_from_nlu_extract_are_validated_and_their_amounts_kept_as_money() -> None:
    loan = [{"kind": "vehicle", "outstanding_inr": 400000, "years_left": 3}]
    models = Models(slots=(candidate("liabilities", loan, "car loan 4 lakh 3 years"),))
    t = s2_turn("a car loan, 4 lakh, 3 years", models=models, prompt="s2.ask:liabilities")
    await run(t)
    want = [{"kind": "vehicle", "outstanding_inr": "400000", "years_left": 3}]
    assert rows(t) == {"liabilities": (want, "proposed")}


@pytest.mark.asyncio
async def test_none_is_zero_and_needs_no_read_back(stored: dict[str, tuple[str, Any]]) -> None:
    for slot in list(NEEDS)[:5]:
        stored[slot] = ("proposed", NEEDS[slot])
    t = s2_turn("none", prompt="s2.ask:existing_cover_inr")
    await run(t)
    assert rows(t) == {"existing_cover_inr": ("0", "proposed")}
    assert ids(t)[-1] == "template:RL-S2-EMPLOYER-COVER"


@pytest.mark.asyncio
async def test_a_lump_sum_is_read_back_without_a_period() -> None:
    t = s2_turn("50 lakh", prompt="s2.ask:existing_cover_inr")
    await run(t)
    assert t.released is not None and t.released.text == "Just to check: ₹50,00,000. Is that right?"


@pytest.mark.asyncio
async def test_a_slot_action_for_another_question_is_ignored() -> None:
    action = {"type": "SLOT", "payload": {"slot": "income_type", "value": "salaried"}}
    t = s2_turn(action=action, prompt="s2.ask:goals")
    await run(t)
    assert rows(t) == {} and ids(t)[-1] == "template:RL-S2-GOALS"


# --- the summary and the binding (I2) -------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_slot_answered_evaluates_once_and_shows_the_summary(
    stored: dict[str, tuple[str, Any]], audited: Recorder
) -> None:
    tier = Tier()
    answered(stored)
    t = s2_turn("yes", tier=tier, prompt="s2.confirm:premium_budget_inr_pa")

    await run(t)

    assert ids(t) == ["template:needs_summary", "template:summary_question"]
    assert t.released is not None and t.released.text == f"{SUMMARY}\n\nIs this correct?"
    (request,) = tier.bodies(EVALUATE)
    sent = request["needs"]
    digest = s2.needs_sha256(NeedsPayload.model_validate(sent))
    assert sent["slots_sha256"] == digest
    assert sent["earmarked_assets_inr"] is None and "cover_to_age" not in sent
    (decision,) = events(audited, "ENGINE_DECISION")
    assert decision["header"].service == "suitability"
    assert decision["header"].inputs_sha256 == digest
    assert t.next is not None and t.next.last_prompt_id == "s2.readback"
    assert t.next.needs is not None and t.next.needs.slots_sha256 is None  # not bound yet
    assert t.next.suitability is not None and t.next.suitability.inputs_sha256 == digest
    assert transition(t) == ("S2.STAY", FsmState.S2)  # no S2 row before the binding


@pytest.mark.asyncio
async def test_confirming_the_summary_binds_the_hash_and_s2_2_moves_to_s3(
    stored: dict[str, tuple[str, Any]], audited: Recorder
) -> None:
    held = await summarised(stored)

    t = await confirm(stored, held)

    assert t.next is not None and t.next.needs is not None and t.next.suitability is not None
    assert t.next.needs.slots_sha256 == t.next.suitability.inputs_sha256
    assert all(status == "confirmed" for _, status in rows(t).values())
    assert set(rows(t)) == set(NEEDS)
    assert transition(t) == ("S2.2", FsmState.S3)
    assert t.transition is not None and t.transition.invariants["I2"]
    assert ids(t) == ["template:needs_done"]
    assert len(events(audited, "ENGINE_DECISION")) == 1  # the summary's, never a second


@pytest.mark.asyncio
async def test_the_summary_answered_no_asks_which_detail(
    stored: dict[str, tuple[str, Any]],
) -> None:
    held = await summarised(stored)
    t = await confirm(stored, held, "no")
    assert ids(t) == ["template:readback_fix"]
    assert [r["label"] for r in t.quick_replies][:2] == ["Goals", "Income"]
    assert transition(t) == ("S2.STAY", FsmState.S2)


@pytest.mark.asyncio
async def test_a_changed_answer_after_the_summary_drops_the_record(
    stored: dict[str, tuple[str, Any]],
) -> None:
    held = await summarised(stored)
    models = Models(slots=(candidate("annual_income_inr", 1500000, "15 lakh"),))
    t = await confirm(stored, held, "actually my income is 15 lakh", models=models)
    assert rows(t) == {"annual_income_inr": ("1500000", "corrected")}
    assert t.next is not None and t.next.needs is None and t.next.suitability is None
    assert t.released is not None and t.released.text == (
        "Just to check: ₹15,00,000 a year. Is that right?"
    )


@pytest.mark.asyncio
async def test_no_gap_exits_advisory_with_no_product(stored: dict[str, tuple[str, Any]]) -> None:
    held = await summarised(stored, Tier(outcome="NO_GAP", reason_codes=["SUIT-NO-GAP"]))
    t = await confirm(stored, held)
    assert transition(t) == ("S2.1", FsmState.EXIT_ADVISORY)
    assert ids(t) == ["template:no_gap"] and t.quick_replies == []


@pytest.mark.asyncio
async def test_red_affordability_escalates(stored: dict[str, tuple[str, Any]]) -> None:
    red = Tier(outcome="ESCALATE", escalation_reason="HE_AFFORDABILITY_RED", affordability="red")
    held = await summarised(stored, red)
    t = await confirm(stored, held)
    assert transition(t) == ("S2.1b", FsmState.HUMAN_ESCALATION)
    assert t.transition is not None and t.transition.reason_code == "HE_AFFORDABILITY_RED"
    assert ids(t) == ["template:advisor_consent_ask"]  # P1 only: P2 asked before any hand-off


@pytest.mark.asyncio
async def test_below_the_sufficiency_minimum_an_election_is_recorded_then_s3(
    stored: dict[str, tuple[str, Any]], audited: Recorder
) -> None:
    held = await summarised(stored, Tier(profile_sufficiency=0.65), dependants=None)
    offered = await confirm(stored, held)
    assert ids(offered) == ["template:partial_offer"]
    assert transition(offered) == ("S2.STAY", FsmState.S2)
    assert offered.next is not None

    t = await confirm(stored, offered.next, action={"type": "ELECT", "payload": {"elected": True}})

    (election,) = events(audited, "SUFFICIENCY_ELECTION")
    header = election["header"]
    assert (header.score, header.threshold, header.elected) == (0.65, 0.7, True)
    assert header.missing_slot_count == 2  # dependants declined, assets declined
    assert transition(t) == ("S2.2", FsmState.S3)
    assert ids(t) == ["template:needs_done", "template:partial_profile"]


@pytest.mark.asyncio
async def test_declining_the_election_asks_the_unanswered_question_again(
    stored: dict[str, tuple[str, Any]], audited: Recorder
) -> None:
    held = await summarised(stored, Tier(profile_sufficiency=0.65), dependants=None)
    offered = await confirm(stored, held)
    assert offered.next is not None

    t = await confirm(stored, offered.next, "no")

    (election,) = events(audited, "SUFFICIENCY_ELECTION")
    assert election["header"].elected is False
    assert ids(t)[-1] == "template:RL-S2-DEPENDANTS"
    assert transition(t) == ("S2.STAY", FsmState.S2)


@pytest.mark.asyncio
async def test_amber_affordability_needs_explicit_confirmation(
    stored: dict[str, tuple[str, Any]],
) -> None:
    held = await summarised(stored, Tier(affordability="amber"))
    asked = await confirm(stored, held)
    assert ids(asked) == ["template:amber_confirm"]
    assert transition(asked) == ("S2.STAY", FsmState.S2)
    assert asked.next is not None

    t = await confirm(stored, asked.next, "yes")

    assert transition(t) == ("S2.2", FsmState.S3)
    assert t.signals["amber_confirmed"] is True


@pytest.mark.asyncio
async def test_amber_declined_offers_an_advisor(stored: dict[str, tuple[str, Any]]) -> None:
    held = await summarised(stored, Tier(affordability="amber"))
    asked = await confirm(stored, held)
    assert asked.next is not None
    t = await confirm(stored, asked.next, "no")
    assert ids(t) == ["template:advisor_offer"]
    assert t.next is not None and t.next.last_prompt_id == "s2.advisor"


@pytest.mark.asyncio
async def test_a_partial_amber_profile_elects_then_confirms_then_s3(
    stored: dict[str, tuple[str, Any]],
) -> None:
    tier = Tier(profile_sufficiency=0.65, affordability="amber")
    held = await summarised(stored, tier, dependants=None)
    offered = await confirm(stored, held)
    assert offered.next is not None
    elect = {"type": "ELECT", "payload": {"elected": True}}
    elected = await confirm(stored, offered.next, action=elect)
    assert ids(elected) == ["template:amber_confirm"]
    assert transition(elected) == ("S2.STAY", FsmState.S2)
    assert elected.next is not None

    t = await confirm(
        stored, elected.next, action={"type": "CONFIRM", "payload": {"confirmed": True}}
    )

    assert transition(t) == ("S2.2", FsmState.S3)


@pytest.mark.asyncio
async def test_implausible_values_are_checked_on_the_summary_then_flagged(
    stored: dict[str, tuple[str, Any]],
) -> None:
    tier = Tier(reason_codes=["IMPLAUSIBLE_INPUT"])
    answered(stored)
    t = s2_turn("yes", tier=tier, prompt="s2.confirm:premium_budget_inr_pa")
    await run(t)
    assert ids(t) == [
        "template:needs_summary",
        "template:summary_implausible",
        "template:summary_question",
    ]
    keep(stored, t)
    assert t.next is not None
    held = t.next
    done = await confirm(stored, held)
    assert transition(done) == ("S2.2", FsmState.S3)  # confirmed: proceed, the flag is kept
    assert done.next is not None and done.next.suitability is not None
    assert "IMPLAUSIBLE_INPUT" in done.next.suitability.reason_codes


# --- the short path -------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_just_tell_me_the_best_plan_offers_three_questions_then_the_defaults(
    stored: dict[str, tuple[str, Any]],
) -> None:
    offered = s2_turn("just tell me the best plan", prompt="s2.ask:goals")
    await run(offered)
    assert ids(offered) == ["template:short_path_offer"]
    assert offered.next is not None and offered.next.last_prompt_id == "s2.short"

    taken = s2_turn("yes", prompt="s2.short", counters=offered.next.counters)
    await run(taken)
    assert ids(taken)[-1] == "template:RL-S2-INCOME"
    assert taken.next is not None and taken.next.counters["short_path"] == 1

    for slot in ("annual_income_inr", "dependants", "liabilities"):
        stored[slot] = ("proposed", NEEDS[slot])
    tier = Tier()
    t = s2_turn("yes", tier=tier, prompt="s2.confirm:liabilities", counters=taken.next.counters)
    await run(t)

    assert ids(t) == [
        "template:needs_summary",
        "template:summary_assumed",
        "template:summary_question",
    ]
    (request,) = tier.bodies(EVALUATE)
    sent = {k: v for k, v in request["needs"].items() if k != "slots_sha256"}
    assert sent == {
        "goals": ["income_protection"],
        "annual_income_inr": "2400000",
        "income_type": "salaried",
        "dependants": NEEDS["dependants"],
        "liabilities": NEEDS["liabilities"],
        "existing_annual_premium_inr": "0",
        "financial_distress": False,
        "comprehension_difficulty_count": 0,
    }
    assert (
        t.released is not None and "the goal is to protect your family's income" in t.released.text
    )


# --- declines -------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_declined_income_is_null_and_the_summary_says_affordability_is_unknown(
    stored: dict[str, tuple[str, Any]],
) -> None:
    tier = Tier()
    held = await summarised(stored, tier, annual_income_inr=None)
    (request,) = tier.bodies(EVALUATE)
    assert request["needs"]["annual_income_inr"] is None
    assert held.suitability is not None and held.suitability.affordability == "unknown"
    t = s2_turn("yes", tier=Tier(), prompt="s2.confirm:premium_budget_inr_pa")
    await run(t)
    assert t.released is not None
    assert "Annual income: not shared, so I can't check what is affordable" in t.released.text


@pytest.mark.asyncio
async def test_declined_dependants_are_left_out_of_the_needs(
    stored: dict[str, tuple[str, Any]],
) -> None:
    tier = Tier()
    await summarised(stored, tier, dependants=None, existing_annual_premium_inr=None)
    (request,) = tier.bodies(EVALUATE)
    assert "dependants" not in request["needs"]
    assert request["needs"]["existing_annual_premium_inr"] == "0"  # required: declined is none


# --- edge cases -----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_distress_slows_down_offers_a_pause_or_an_advisor_and_is_recorded() -> None:
    t = s2_turn("I lost my job last month", prompt="s2.ask:goals", models=Models())
    await run(t)
    assert ids(t) == ["template:distress_ack", "template:RL-S2-GOALS"]  # no generated sentence
    assert rows(t) == {"financial_distress": (True, "proposed")}
    types = [r["action"]["type"] for r in t.quick_replies]
    assert "HUMAN_REQUEST" in types and "CONTINUE" in types


@pytest.mark.asyncio
async def test_distress_reaches_the_needs_for_the_vulnerability_rules(
    stored: dict[str, tuple[str, Any]],
) -> None:
    stored["financial_distress"] = ("proposed", True)
    tier = Tier()
    await summarised(stored, tier)
    (request,) = tier.bodies(EVALUATE)
    assert request["needs"]["financial_distress"] is True


@pytest.mark.asyncio
async def test_comprehension_difficulty_is_counted_and_the_question_put_more_simply() -> None:
    t = s2_turn("I don't understand", prompt="s2.ask:dependants")
    await run(t)
    assert ids(t) == ["template:rephrase", "template:RL-S2-DEPENDANTS"]
    assert t.next is not None and t.next.counters["comprehension_difficulty"] == 1
    assert t.released is not None and SLOTS["RL-S2-DEPENDANTS"].hint in t.released.text


@pytest.mark.asyncio
async def test_the_comprehension_count_reaches_the_needs(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored)
    tier = Tier()
    t = s2_turn("yes", tier=tier, prompt="s2.confirm:premium_budget_inr_pa",
                counters={"comprehension_difficulty": 2})  # fmt: skip
    await run(t)
    (request,) = tier.bodies(EVALUATE)
    assert request["needs"]["comprehension_difficulty_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said", "note"),
    [
        ("I want guaranteed high returns", "guarantee_note"),
        ("is the lic plan better?", "competitor_note"),
    ],
)
async def test_guarantees_and_other_insurers_get_a_note_and_the_question_again(
    said: str, note: str
) -> None:
    t = s2_turn(said, prompt="s2.ask:goals")
    await run(t)
    assert ids(t) == [f"template:{note}", "template:RL-S2-GOALS"]
    assert t.released is not None and "guaranteed return" not in t.released.text.casefold()
    if note == "competitor_note":
        assert BUNDLE.manifest.insurer in t.released.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slot", "said"), [("income_type", "homemaker"), ("annual_income_inr", "no income")]
)
async def test_no_personal_income_explains_the_non_earning_basis(
    stored: dict[str, tuple[str, Any]], slot: str, said: str
) -> None:
    stored["goals"] = ("proposed", ["income_protection"])
    if slot == "income_type":
        stored["annual_income_inr"] = ("proposed", "0")
    t = s2_turn(said, prompt=f"s2.ask:{slot}")
    await run(t)
    assert "template:non_earning_basis" in ids(t)


# --- dependency down, hydration, I1 ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_suitability_service_down_pauses_and_keeps_the_answers(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored)
    t = s2_turn("yes", tier=Tier(down=(EVALUATE,)), prompt="s2.confirm:premium_budget_inr_pa")
    await run(t)
    assert t.transition is not None
    assert (t.transition.row_id, t.transition.reason_code) == ("CC3", "DEPENDENCY_DOWN")
    assert t.next is not None and t.next.fsm_state is FsmState.PAUSE
    assert ids(t) == ["template:dependency_down", "template:paused"]


@pytest.mark.asyncio
async def test_a_result_for_other_inputs_is_never_bound(
    stored: dict[str, tuple[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    answered(stored)
    tier = Tier(inputs_sha256="0" * 64)
    t = s2_turn("yes", tier=tier, prompt="s2.confirm:premium_budget_inr_pa")
    with caplog.at_level(logging.ERROR, logger="surakshasetu.graph.states.s2"):
        await run(t)
    assert "I2" in caplog.text
    assert t.next is not None and t.next.needs is None and t.next.suitability is None
    assert t.next.fsm_state is FsmState.PAUSE  # never S3


@pytest.mark.asyncio
async def test_entering_s2_while_the_service_is_down_offers_a_retry() -> None:
    t = s2_turn(tier=Tier(down=("/v1/suitability",)))
    t.parts = [("screening_done", SCRIPTS.screening_done)]
    await s2.enter(GraphState(), runtime=rt(t))
    assert [i for i, _ in t.parts] == ["screening_done", "needs_retry"]
    assert [r["action"]["type"] for r in t.quick_replies] == ["RETRY"]


@pytest.mark.asyncio
async def test_after_hydration_eligibility_is_recomputed_before_suitability(
    stored: dict[str, tuple[str, Any]], audited: Recorder
) -> None:
    answered(stored)
    tier = Tier()
    t = s2_turn("yes", tier=tier, prompt="s2.confirm:premium_budget_inr_pa", eligibility=None)
    await run(t)
    services = [e["header"].service for e in events(audited, "ENGINE_DECISION")]
    assert services == ["eligibility", "suitability"]
    assert ids(t)[0] == "template:needs_summary"


@pytest.mark.asyncio
async def test_without_valid_consent_nothing_is_asked_or_called(audited: Recorder) -> None:
    tier = Tier()
    t = s2_turn("24 lakh", tier=tier, prompt="s2.ask:annual_income_inr", consent=None)
    await run(t)
    # Only S0's re-entry reads the notice and the AI disclosure (reference reads, allowed).
    assert not [p for p, _ in tier.calls if p.startswith(("/v1/suitability", "/v1/eligibility"))]
    assert t.slot_rows == [] and audited.types().count("ENGINE_DECISION") == 0
    assert transition(t) == ("G1", FsmState.S0)


# --- V4 from S3 -----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_needs_fact_corrected_in_s3_re_runs_s2s_rows_with_a_new_hash(
    stored: dict[str, tuple[str, Any]],
) -> None:
    held = await summarised(stored)
    bound = await confirm(stored, held)
    assert bound.next is not None and bound.next.fsm_state is FsmState.S3
    first = bound.next.suitability
    assert first is not None

    kids = [{"relation": "spouse", "age": 32}, {"relation": "child", "age": 4},
            {"relation": "child", "age": 7}]  # fmt: skip
    models = Models(slots=(candidate("dependants", kids, "two kids, 4 and 7"),))
    tier = Tier()
    t = s2_turn(
        "actually I have two kids, 4 and 7",
        models=models,
        tier=tier,
        state=FsmState.S3,
        needs=bound.next.needs,
        suitability=first,
    )
    await run(t)

    assert rows(t) == {"dependants": (kids, "corrected")}
    assert transition(t) == ("G3", FsmState.S2)
    assert ids(t) == ["template:needs_summary", "template:summary_question"]  # s2_enter
    assert t.next is not None and t.next.suitability is not None
    assert t.next.suitability.inputs_sha256 != first.inputs_sha256
    assert t.next.needs is not None and t.next.needs.slots_sha256 is None  # confirm again (I2)


# --- logs -----------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_needs_value_reaches_the_logs(
    stored: dict[str, tuple[str, Any]],
    caplog: pytest.LogCaptureFixture,
    restore_logging: None,
) -> None:
    answered(stored, annual_income_inr="2468000")
    with caplog.at_level(logging.DEBUG):
        held = await summarised(stored, annual_income_inr="2468000")
        await confirm(stored, held)
        t = s2_turn("home loan of 37 lakh, 19 years left", prompt="s2.ask:liabilities")
        await run(t)
    for sentinel in ("2468000", "24,68,000", "3700000", "37,00,000", "37 lakh"):
        assert sentinel not in caplog.text
    assert nodes is not None


@pytest.mark.asyncio
async def test_an_answer_in_the_same_message_as_distress_is_still_taken() -> None:
    t = s2_turn("my wife is 32, and honestly I lost my job last month", prompt="s2.ask:dependants")
    await run(t)
    assert rows(t) == {
        "financial_distress": (True, "proposed"),
        "dependants": ([{"relation": "spouse", "age": 32}], "proposed"),
    }
    assert ids(t) == ["template:distress_ack", "template:RL-S2-GOALS"]  # the first missing one
