"""State-1 (Step 19): the rules' questions in order with their reasons and one generated sentence,
evidenced slot filling with the confidence bands, the minor path before persistence, the read-back,
the Eligibility Service's outcomes through S1's rows, the edge cases, the dependency-down retry, I1,
and the V4 correction hook. The domain tier is runtime_support's MockTransport stand-in; the store's
slot reads are patched; audit appends are recorded."""

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
from runtime_support import FRIENDLY, Models, domain, domain_handler
from test_runtime_nodes import BUNDLE, Recorder, rt, run, session, turn
from test_s0 import ids, transition, valid_record

from surakshasetu.compose.bundle import ScreeningLexicon
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import handlers, nodes, states
from surakshasetu.graph.nodes import Turn
from surakshasetu.graph.state import EligibilityPayload, GraphState, SlotRow
from surakshasetu.graph.states import s1
from surakshasetu.store import conv as store

SCRIPTS = BUNDLE.templates["en-IN"].scripts
SLOTS = BUNDLE.templates["en-IN"].slots
EVALUATE = "/v1/eligibility/evaluate"
# Every attribute answered, as the TDD example: 34, resident, Pune, no tobacco, salaried, no to the
# health question, cover for their own life.
TDD = {
    "age_years": 34,
    "residency": "resident",
    "pincode": "411001",
    "tobacco_12m": False,
    "occupation_code": "OCC-OFFICE-01",
    "health_flags": {"RL-S1-HEALTH": False},
    "proposer.is_life_assured": True,
}
READBACK = (
    "To confirm: 34, resident in India, Pune, no tobacco in the last 12 months, Salaried office"
    " professional, no to the health question, cover for your own life. Correct?"
)


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


@pytest.fixture(autouse=True)
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, Any]]:
    """The slot rows already in conv.slot_value (newest per slot); none unless a test adds them."""
    rows: dict[str, tuple[str, Any]] = {}
    monkeypatch.setattr(store, "latest_slots", lambda *a: dict(rows))
    monkeypatch.setattr(handlers, "_PRODUCT_NAMES", {})
    return rows


class Tier:
    """runtime_support's domain stand-in, recording every call. `down` lists path prefixes that
    are unreachable."""

    def __init__(self, *, down: tuple[str, ...] = ()) -> None:
        self.down = down
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        if any(request.url.path.startswith(d) for d in self.down):
            raise httpx.ConnectError("down")
        return domain_handler(request)

    def bodies(self, path: str) -> list[Any]:
        return [b for p, b in self.calls if p == path]


def s1_turn(
    text: str | None = None,
    *,
    action: dict[str, Any] | None = None,
    models: Models | None = None,
    tier: Tier | None = None,
    prompt: str | None = None,
    state: FsmState = FsmState.S1,
    **update: Any,
) -> Turn:
    t = turn(models, text=text)
    t.action = action
    t.domain = domain(tier or Tier())
    t.next = session(
        **({"fsm_state": state, "consent": valid_record(), "last_prompt_id": prompt} | update)
    )
    t.row = t.row.__class__(**{**t.row.__dict__, "fsm_state": state.value})
    return t


def answered(stored: dict[str, tuple[str, Any]], status: str = "proposed", **values: Any) -> None:
    for slot, value in (TDD | {k.replace("__", "."): v for k, v in values.items()}).items():
        stored[slot] = ("declined" if value is None and status != "confirmed" else status, value)


def candidate(slot: str, value: Any, span: str, confidence: float = 0.95) -> dict[str, Any]:
    return {"slot": slot, "value": value, "confidence": confidence, "evidence_span": span}


async def until_decide(t: Turn) -> None:
    """input -> route -> the S1 node -> decide, without the handler decide routes to."""
    state = GraphState()
    await nodes.input_node(state, rt(t))
    await nodes.route(state, rt(t))
    fsm_state = t.next.fsm_state if t.next else FsmState.S1
    await states.wrapped(fsm_state, states.NODES[fsm_state])(state, runtime=rt(t))
    await nodes.decide(state, rt(t))


def rows(t: Turn) -> dict[str, tuple[Any, str]]:
    return {r.slot: (r.value, r.status) for r in t.slot_rows}


# --- the questions --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_first_question_is_the_rules_first_with_its_reason_and_one_friendly_sentence(
    audited: Recorder,
) -> None:
    models = Models()
    t = s1_turn("hello", models=models)

    await run(t)

    assert ids(t) == ["generated:narrative", "template:RL-S1-AGE"]
    age = SLOTS["RL-S1-AGE"]
    assert t.released is not None
    assert t.released.text == f"{FRIENDLY}\n\n{age.question} {age.reason}"
    assert t.next is not None and t.next.last_prompt_id == "s1.ask:age_years"
    assert t.next.pending_slot == "age_years"
    assert "gen-converse" in models.routes and "MODEL_CALL" in audited.types()
    assert transition(t) == ("S1.STAY", FsmState.S1)


@pytest.mark.asyncio
async def test_the_question_goes_out_alone_when_gen_converse_is_down() -> None:
    t = s1_turn("hello", models=Models(down=("gen-converse",)))
    await run(t)
    assert ids(t) == ["template:RL-S1-AGE"]


@pytest.mark.asyncio
async def test_a_draft_naming_a_product_is_regenerated_once_then_the_template_alone() -> None:
    """I3: before S3 no product name, even in a friendly sentence."""
    named = "Suraksha Term Shield would suit you."
    t = s1_turn("hello", models=Models(converse=(named, named)))

    await run(t)

    assert t.released is not None and t.released.kind == "fallback"
    assert ids(t) == ["template:RL-S1-AGE"] and "Suraksha" not in t.released.text


@pytest.mark.asyncio
async def test_conditional_questions_follow_the_rules_asked_if(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored, proposer__is_life_assured=False)
    t = s1_turn("no", prompt="s1.ask:proposer.is_life_assured")
    await run(t)
    assert ids(t)[-1] == "template:RL-S1-LA-RELATIONSHIP"
    labels = [r["label"] for r in t.quick_replies]
    assert labels == ["Spouse", "Child", "Parent", "Someone else"]


def test_an_unknown_asked_if_form_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        s1._applies("proposer.la_age > 18", {})


# --- filling slots --------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_evidenced_value_is_proposed_and_the_next_question_asked() -> None:
    models = Models(slots=(candidate("age_years", 34, "34 years old"),))
    t = s1_turn("I am 34 years old", models=models, prompt="s1.ask:age_years")

    await run(t)

    assert rows(t) == {"age_years": (34, "proposed")}
    assert ids(t)[-1] == "template:RL-S1-RESIDENCY"


@pytest.mark.asyncio
@pytest.mark.parametrize(("said", "age"), [("34", 34), ("३४", 34), ("मैं 47 का हूँ", 47)])
async def test_the_question_just_asked_is_read_from_the_whole_message(said: str, age: int) -> None:
    t = s1_turn(said, prompt="s1.ask:age_years")
    await run(t)
    assert t.slot_rows == [SlotRow("age_years", age, 1.0, "proposed")]


@pytest.mark.asyncio
async def test_a_value_between_the_floor_and_the_accept_band_is_read_back_at_once() -> None:
    models = Models(slots=(candidate("age_years", 34, "34", confidence=0.7),))
    t = s1_turn("34 I think", models=models, prompt="s1.ask:age_years")

    await run(t)

    assert rows(t) == {"age_years": (34, "proposed")}
    assert ids(t) == ["template:readback"]
    assert t.released is not None and t.released.text == "To confirm: 34. Correct?"
    assert t.next is not None and t.next.last_prompt_id == "s1.confirm:age_years"


@pytest.mark.asyncio
async def test_a_yes_to_a_one_value_read_back_is_never_a_yes_no_answer(
    stored: dict[str, tuple[str, Any]],
) -> None:
    """At 0.7, "tobacco: yes" is read back; the "yes" that follows confirms it, nothing more."""
    stored.update({"age_years": ("proposed", 34), "tobacco_12m": ("proposed", False)})
    t = s1_turn("yes", prompt="s1.confirm:tobacco_12m")
    await run(t)
    assert t.slot_rows == [] and ids(t)[-1] == "template:RL-S1-RESIDENCY"


@pytest.mark.asyncio
async def test_a_value_below_the_floor_is_dropped_and_counts_toward_the_streak() -> None:
    models = Models(slots=(candidate("age_years", 34, "34", confidence=0.5),))
    t = s1_turn("maybe 34 or so", models=models, prompt="s1.ask:age_years")

    await run(t)

    assert t.slot_rows == []
    assert t.next is not None and t.next.counters["low_confidence_streak"] == 1
    assert ids(t) == ["template:format_hint", "template:RL-S1-AGE"]


@pytest.mark.asyncio
async def test_an_age_from_a_birth_year_is_read_back_before_it_is_used() -> None:
    models = Models(slots=(candidate("age_years", None, "born in '91"),))
    t = s1_turn("I was born in '91", models=models, prompt="s1.ask:age_years")

    await run(t)

    age = datetime.now(ZoneInfo("Asia/Kolkata")).year - 1991
    assert rows(t) == {"age_years": (age, "proposed")}
    assert t.next is not None and t.next.last_prompt_id == "s1.confirm:age_years"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "models",
    [Models(slots=(candidate("age_years", 16, "16"),)), Models()],
    ids=["nlu", "whole message"],
)
async def test_an_age_under_18_erases_before_anything_is_written(models: Models) -> None:
    t = s1_turn("16", models=models, prompt="s1.ask:age_years")

    await until_decide(t)

    assert t.slot_rows == [] and t.signals.get("minor") is True
    assert transition(t) == ("CC1b", FsmState.DATA_ERASURE)


@pytest.mark.asyncio
async def test_a_quick_reply_answers_only_the_open_question() -> None:
    t = s1_turn(
        action={"type": "SLOT", "payload": {"slot": "residency", "value": "nri"}},
        prompt="s1.ask:residency",
    )
    await run(t)
    assert rows(t) == {"residency": ("nri", "proposed")}

    other = s1_turn(
        action={"type": "SLOT", "payload": {"slot": "residency", "value": "nri"}},
        prompt="s1.ask:age_years",
    )
    await run(other)
    assert other.slot_rows == [] and ids(other)[-1] == "template:RL-S1-AGE"


@pytest.mark.asyncio
async def test_an_occupation_is_a_code_from_the_master_and_several_matches_are_offered() -> None:
    t = s1_turn("salaried", prompt="s1.ask:occupation_code")
    await run(t)
    assert rows(t) == {"occupation_code": ("OCC-OFFICE-01", "proposed")}

    several = s1_turn("e", prompt="s1.ask:occupation_code")  # every label has an "e"
    await run(several)
    assert several.slot_rows == [] and ids(several)[-1] == "template:RL-S1-OCCUPATION"
    assert len(several.quick_replies) == 4
    assert several.quick_replies[0]["action"]["payload"]["value"] == "OCC-OFFICE-01"


# --- the read-back and the engine -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_read_back_states_every_value_from_the_slots(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored)
    t = s1_turn("ok")  # no question open: everything is known

    await run(t)

    assert ids(t) == ["template:readback"]
    assert t.released is not None and t.released.text == READBACK
    assert [r["action"]["payload"] for r in t.quick_replies] == [
        {"confirmed": True},
        {"confirmed": False},
    ]


@pytest.mark.asyncio
async def test_a_confirmed_read_back_writes_confirmed_rows_then_the_engine_decides(
    stored: dict[str, tuple[str, Any]], audited: Recorder
) -> None:
    answered(stored)
    tier = Tier()
    t = s1_turn("yes", tier=tier, prompt="s1.readback")

    await run(t)

    assert {slot: status for slot, (_, status) in rows(t).items()} == dict.fromkeys(
        TDD, "confirmed"
    )
    (request,) = tier.bodies(EVALUATE)
    assert request == {
        "pins": {"rules": "2026.09.1"},
        "age_years": 34,
        "gender": None,
        "residency": "resident",
        "pincode": "411001",
        "tobacco_12m": False,
        "occupation_code": "OCC-OFFICE-01",
        "health_flags": {"RL-S1-HEALTH": False},
        "proposer": {
            "is_life_assured": True,
            "relationship": None,
            "la_age": None,
            "business_cover": None,
        },
    }
    decision = next(e for e in audited.events if e["event_type"].value == "ENGINE_DECISION")
    assert decision["header"].service == "eligibility"
    assert set(decision["payload"]) == {"request", "result"}
    assert transition(t) == ("S1.4", FsmState.S2)
    assert ids(t) == ["template:screening_done"]
    assert t.next is not None and isinstance(t.next.eligibility, EligibilityPayload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "row", "reason"),
    [
        ({"residency": "nri"}, "S1.1", "HE_NRI"),
        ({"age_years": 70}, "S1.1", "HE_AGE_BAND"),
        (
            {
                "proposer__is_life_assured": False,
                "proposer__relationship": "sibling",
                "proposer__la_age": 40,
                "proposer__business_cover": False,
            },
            "S1.1",
            "HE_COMPLEX_PROPOSER",
        ),
        ({"pincode": "744101"}, "S1.2", "NOT_ELIGIBLE"),
    ],
)
async def test_the_engine_outcome_routes_by_s1_rows(
    stored: dict[str, tuple[str, Any]], values: dict[str, Any], row: str, reason: str
) -> None:
    answered(stored, "confirmed", **values)
    t = s1_turn(action={"type": "RETRY", "payload": {}})

    await until_decide(t)

    assert t.transition is not None
    assert (t.transition.row_id, t.transition.reason_code) == (row, reason)


@pytest.mark.asyncio
async def test_not_eligible_explains_with_the_reason_line(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored, "confirmed", pincode="744101")
    t = s1_turn(action={"type": "RETRY", "payload": {}})
    await run(t)
    assert ids(t) == ["template:not_eligible"] and t.released is not None
    assert SCRIPTS.not_eligible_reasons["REASON_PIN_UNSERVICEABLE"] in t.released.text
    assert transition(t) == ("S1.2", FsmState.EXIT)


@pytest.mark.asyncio
async def test_re_ask_asks_the_occupation_once_then_escalates(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored, "confirmed", occupation_code=None)
    first = s1_turn(action={"type": "RETRY", "payload": {}})
    await run(first)
    assert transition(first) == ("S1.STAY", FsmState.S1)
    assert ids(first) == ["template:reask", "template:RL-S1-OCCUPATION"]
    assert first.next is not None and first.next.counters["occupation_reask"] == 1

    again = s1_turn(action={"type": "RETRY", "payload": {}}, counters={"occupation_reask": 1})
    await until_decide(again)
    assert again.transition is not None and again.transition.row_id == "S1.1b"
    assert again.transition.reason_code == "HE_RE_ASK_LIMIT"

    # Declined again, in answer to the re-ask: no new row (the same value), the engine again.
    assert first.next is not None
    declined = s1_turn(
        "I'd prefer not to say",
        prompt="s1.ask:occupation_code",
        counters={"occupation_reask": 1},
        eligibility=first.next.eligibility,
    )
    await until_decide(declined)
    assert declined.slot_rows == []
    assert declined.transition is not None and declined.transition.row_id == "S1.1b"


@pytest.mark.asyncio
async def test_a_declined_tobacco_answer_is_eligible_with_the_premium_withheld(
    stored: dict[str, tuple[str, Any]],
) -> None:
    t = s1_turn("I'd prefer not to say", prompt="s1.ask:tobacco_12m")
    await run(t)
    assert rows(t) == {"tobacco_12m": (None, "declined")}

    answered(stored, "confirmed", tobacco_12m=None)
    decided = s1_turn(action={"type": "RETRY", "payload": {}})
    await run(decided)
    assert transition(decided) == ("S1.4", FsmState.S2)
    engine = decided.next.eligibility.engine if decided.next and decided.next.eligibility else None
    assert engine is not None and engine.flags == ["PREMIUM_WITHHELD"]


@pytest.mark.asyncio
async def test_an_early_price_request_offers_the_express_path_and_eligibility_leads_there(
    stored: dict[str, tuple[str, Any]],
) -> None:
    t = s1_turn(
        "how much will it cost?",
        models=Models(intents=("EXPRESS_PATH",)),
        prompt="s1.ask:age_years",
    )
    await run(t)
    assert ids(t) == ["template:express_offer", "template:RL-S1-AGE"]
    assert t.next is not None and t.next.counters["express_path"] == 1

    answered(stored, "confirmed")
    decided = s1_turn(action={"type": "RETRY", "payload": {}}, counters={"express_path": 1})
    await run(decided)
    assert transition(decided) == ("S1.3", FsmState.QUOTE_ONLY)
    assert ids(decided) == ["template:plan_ask"]  # Quote-Only's enter: the customer names a plan
    assert decided.next is not None and "express_path" not in decided.next.counters


# --- edge cases -----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_serious_illness_disclosed_gets_the_acknowledgement_and_an_advisor_offer(
    stored: dict[str, tuple[str, Any]],
) -> None:
    stored.update({"age_years": ("proposed", 34)})
    t = s1_turn("yes", prompt="s1.ask:health_flags")

    await run(t)

    assert rows(t) == {"health_flags": ({"RL-S1-HEALTH": True}, "proposed")}
    assert ids(t)[:1] == ["template:medical_ack"]
    assert any(r["action"]["type"] == "HUMAN_REQUEST" for r in t.quick_replies)


@pytest.mark.asyncio
async def test_a_question_about_a_condition_gets_the_underwriting_note_and_no_speculation() -> None:
    t = s1_turn("Will I be rejected because of diabetes?", prompt="s1.ask:tobacco_12m")
    await run(t)
    assert ids(t) == ["template:underwriting_note", "template:RL-S1-TOBACCO"]
    assert t.slot_rows == []


@pytest.mark.asyncio
async def test_a_request_to_hide_a_material_fact_gets_the_neutral_note() -> None:
    models = Models(slots=(candidate("tobacco_12m", True, "I smoke"),))
    t = s1_turn("I smoke but don't tell them", models=models, prompt="s1.ask:tobacco_12m")
    await run(t)
    assert ids(t)[0] == "template:nondisclosure_note"
    assert rows(t) == {"tobacco_12m": (True, "proposed")}  # what was said is still recorded


@pytest.mark.asyncio
async def test_off_topic_gets_a_one_line_redirect_and_the_question_again() -> None:
    t = s1_turn("nice weather", models=Models(intents=("OFF_TOPIC",)), prompt="s1.ask:pincode")
    await run(t)
    assert ids(t) == ["template:redirect", "template:RL-S1-PINCODE"]
    assert t.next is not None and t.next.counters.get("invalid_input", 0) == 0


@pytest.mark.asyncio
async def test_invalid_input_gets_the_hint_then_the_advisor_offer_at_the_limit() -> None:
    first = s1_turn("blah", prompt="s1.ask:pincode")
    await run(first)
    assert ids(first) == ["template:format_hint", "template:RL-S1-PINCODE"]
    assert first.released is not None and SLOTS["RL-S1-PINCODE"].hint in first.released.text

    third = s1_turn("blah", prompt="s1.ask:pincode", counters={"invalid_input": 2})
    await run(third)
    assert ids(third) == ["template:advisor_offer"]
    assert [r["action"]["type"] for r in third.quick_replies] == ["HUMAN_REQUEST", "CONTINUE"]

    yes = s1_turn("yes", prompt="s1.advisor")
    await until_decide(yes)
    assert yes.transition is not None and yes.transition.row_id == "CC2"


@pytest.mark.asyncio
async def test_an_unknown_pincode_is_invalid_input() -> None:
    t = s1_turn("999999", prompt="s1.ask:pincode")
    await run(t)
    assert t.slot_rows == [] and ids(t)[0] == "template:format_hint"


@pytest.mark.asyncio
async def test_a_wrong_read_back_asks_which_detail_then_that_question(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored)
    t = s1_turn("no", prompt="s1.readback")
    await run(t)
    assert ids(t) == ["template:readback_fix"]
    assert t.quick_replies[2]["action"] == {"type": "REASK", "payload": {"slot": "pincode"}}

    chosen = s1_turn(action={"type": "REASK", "payload": {"slot": "pincode"}}, prompt="s1.fix")
    await run(chosen)
    assert ids(chosen)[-1] == "template:RL-S1-PINCODE"


# --- dependency down, I1, V4 ----------------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_domain_tier_down_gives_the_retry_and_never_proceeds() -> None:
    t = s1_turn("34", tier=Tier(down=("/v1/eligibility",)), prompt="s1.ask:age_years")
    await run(t)
    assert ids(t) == ["template:screening_retry"]
    assert [r["action"]["type"] for r in t.quick_replies] == ["RETRY"]
    assert transition(t) == ("S1.STAY", FsmState.S1)


@pytest.mark.asyncio
async def test_the_engine_down_keeps_the_confirmation_and_retry_evaluates(
    stored: dict[str, tuple[str, Any]],
) -> None:
    answered(stored)
    down = s1_turn("yes", tier=Tier(down=(EVALUATE,)), prompt="s1.readback")
    await run(down)
    assert ids(down) == ["template:screening_retry"]
    assert {status for _, status in rows(down).values()} == {"confirmed"}

    answered(stored, "confirmed")
    retried = s1_turn(action={"type": "RETRY", "payload": {}})
    await run(retried)
    assert transition(retried) == ("S1.4", FsmState.S2)


@pytest.mark.asyncio
async def test_without_valid_consent_nothing_is_asked_or_written() -> None:
    """I1: no domain call and no slot row; G1 re-enters S0."""
    tier = Tier()
    t = s1_turn("I am 34", tier=tier, prompt="s1.ask:age_years")
    assert t.next is not None
    t.next.consent = None

    await until_decide(t)

    assert tier.calls == [] and t.slot_rows == []
    assert transition(t) == ("G1", FsmState.S0)


@pytest.mark.asyncio
async def test_a_corrected_eligibility_fact_in_s2_re_runs_s1_with_a_read_back(
    stored: dict[str, tuple[str, Any]],
) -> None:
    """V4: a new row, the engine's result dropped, G2 -> S1, and everything read back."""
    answered(stored, "confirmed")
    models = Models(intents=("CORRECTION",), slots=(candidate("pincode", "411014", "411014"),))
    t = s1_turn("my pincode is actually 411014", models=models, state=FsmState.S2)

    await run(t)

    assert rows(t) == {"pincode": ("411014", "corrected")}
    assert transition(t) == ("G2", FsmState.S1)
    assert ids(t) == ["template:readback"]
    assert t.next is not None and t.next.eligibility is None


# --- the bundle's screening lexicon, and the logs -------------------------------------------------
def test_the_screening_lexicon_is_whole_words_and_refuses_non_strings() -> None:
    lexicon = BUNDLE.screening_lexicon
    assert "diabetes" in lexicon.health_terms and "apply now" in lexicon.apply
    with pytest.raises(ValueError, match="non-empty strings"):
        ScreeningLexicon.model_validate({"health_terms": [True], "concealment": [], "apply": []})


@pytest.mark.asyncio
async def test_no_slot_value_reaches_the_logs(
    stored: dict[str, tuple[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    answered(stored, pincode="411014", age_years=47)
    caplog.set_level(logging.DEBUG, logger="surakshasetu")
    for t in (
        s1_turn("411014", prompt="s1.ask:pincode"),
        s1_turn("yes", prompt="s1.readback"),
    ):
        await run(t)
    assert "411014" not in caplog.text and "47" not in caplog.text and "Pune" not in caplog.text
