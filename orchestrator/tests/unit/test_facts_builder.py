"""graph.facts.build_facts: the Step 15 builder contracts, one mapping per test."""

from typing import Any
from uuid import UUID

import pytest

from surakshasetu.analysis.models import Intent, TurnAnalysis
from surakshasetu.config import Settings
from surakshasetu.domain.models import ConsentRecord, EligibilityResult
from surakshasetu.fsm.facts import MandatoryTrigger
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph.facts import build_facts, consent_status
from surakshasetu.graph.state import EligibilityPayload, Frame, SessionState, VersionPins

SETTINGS = Settings(_env_file=None)
ID = UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")


def session(**update: Any) -> SessionState:
    pins = VersionPins(
        prompt_bundle="pb-2026.09.1",
        rules="2026.09.1",
        corpus={},
        consent_notice="2026.09.1-en",
        params="params",
        ranker="ranker",
        registry="2026.09.1",
    )
    return SessionState(
        session_id=ID, subject_ref=str(ID), fsm_state=FsmState.S0, pins=pins
    ).model_copy(update=update)


def record(valid: bool, *reasons: str) -> ConsentRecord:
    return ConsentRecord.model_validate(
        {
            "consent_id": str(ID),
            "notice_version": "2026.09.1-en",
            "notice_sha256": "ab" * 32,
            "notice_language": "en-IN",
            "ai_disclosure_version": "ai-2026.09.1",
            "purposes": [{"purpose_id": "P1_NEEDS_RECO", "granted": True}],
            "age_18_plus_declared": "AGE_NOT_DECLARED" not in reasons,
            "method": "structured_action",
            "captured_at": "2026-09-23T10:15:00Z",
            "valid_p1": valid,
            "valid_reasons": list(reasons),
        }
    )


def analysis(*intents: Intent) -> TurnAnalysis:
    return TurnAnalysis(intents=list(intents), language="en")


@pytest.mark.parametrize(
    ("consent", "expected"),
    [
        (None, "none"),
        (record(True), "valid"),
        (record(False, "P1_NOT_GRANTED"), "refused"),
        (record(False, "CONSENT_EXPIRED"), "lapsed"),
        (record(False, "NOTICE_SUPERSEDED"), "lapsed"),
        (record(False, "WITHDRAWN"), "lapsed"),
    ],
)
def test_consent_comes_from_the_consent_service_record(
    consent: ConsentRecord | None, expected: str
) -> None:
    assert consent_status(consent) == expected
    assert build_facts(session(consent=consent), None, None, SETTINGS).consent == expected


def test_signals_come_from_intents() -> None:
    facts = build_facts(
        session(),
        analysis(
            Intent.META_WITHDRAW,
            Intent.META_HUMAN,
            Intent.FRUSTRATION,
            Intent.NEED_TIME,
            Intent.DECLINE,
            Intent.REJECT_ALL,
            Intent.EXPRESS_PATH,
            Intent.GENERAL_FAQ,
            Intent.OBJECTION_PRICE,
        ),
        None,
        SETTINGS,
    )
    assert facts.withdraw and facts.human_request and facts.frustration
    assert facts.need_time and facts.explicit_decline and facts.all_rejected
    assert facts.express_path and facts.faq and facts.objection
    assert facts.intent == "general_faq"


def test_no_analysis_means_nothing_happened() -> None:
    facts = build_facts(session(), None, None, SETTINGS)
    assert not (facts.withdraw or facts.faq or facts.objection or facts.minor)
    assert facts.intent is None and facts.mandatory_trigger is None


@pytest.mark.parametrize(
    ("intent", "expected"),
    [
        (Intent.NEW_PURCHASE, "new_purchase"),
        (Intent.SPECIFIC_PLAN, "specific_plan"),
        (Intent.EXISTING_POLICY, "existing_policy"),
    ],
)
def test_s0_intents(intent: Intent, expected: str) -> None:
    assert build_facts(session(), analysis(intent), None, SETTINGS).intent == expected


def test_a_safety_block_or_intent_is_the_self_harm_trigger() -> None:
    assert (
        build_facts(session(), None, "safety", SETTINGS).mandatory_trigger
        is MandatoryTrigger.SELF_HARM
    )
    assert (
        build_facts(session(), analysis(Intent.SAFETY), None, SETTINGS).mandatory_trigger
        is MandatoryTrigger.SELF_HARM
    )


def test_the_injection_limit_is_a_mandatory_trigger() -> None:
    below = session(counters={"injection": SETTINGS.injection_hit_limit - 1})
    at = session(counters={"injection": SETTINGS.injection_hit_limit})
    assert build_facts(below, None, "injection", SETTINGS).mandatory_trigger is None
    assert (
        build_facts(at, None, "injection", SETTINGS).mandatory_trigger
        is MandatoryTrigger.INJECTION_LIMIT
    )


def test_counters_feed_the_streak_and_the_loops() -> None:
    facts = build_facts(
        session(counters={"low_confidence_streak": 2, "rediscovery_loops": 1}), None, None, SETTINGS
    )
    assert (facts.low_confidence_streak, facts.rediscovery_loops) == (2, 1)


def test_a_minor_is_the_unticked_18_plus_box_or_the_engine_exit() -> None:
    unticked = record(False, "AGE_NOT_DECLARED")
    assert build_facts(session(consent=unticked), None, None, SETTINGS).minor
    refused = record(False, "P1_NOT_GRANTED", "AGE_NOT_DECLARED")
    assert not build_facts(session(consent=refused), None, None, SETTINGS).minor

    engine = EligibilityResult.model_validate(
        {
            "decision_id": str(ID),
            "outcome": "DATA_ERASURE_EXIT",
            "eligible_uins": [],
            "uw_path": "standard",
            "flags": [],
            "rule_ids": ["E-01"],
            "reason_codes": [],
            "rules_version": "2026.09.1",
            "params_version": "p",
            "inputs_sha256": "cd" * 32,
        }
    )
    payload = EligibilityPayload(
        age_years=18,
        residency="resident",
        pincode="400001",
        tobacco_12m=False,
        occupation_class="OCC-SWE-03",
        health_flags={},
        engine=engine,
    )
    facts = build_facts(session(eligibility=payload), None, None, SETTINGS)
    assert facts.minor
    assert facts.eligibility is not None and facts.eligibility.outcome == "DATA_ERASURE_EXIT"


def test_paused_from_is_the_frame_pushed_on_pause() -> None:
    paused = session(fsm_state=FsmState.PAUSE, stack=[Frame(state=FsmState.S2)])
    assert build_facts(paused, None, None, SETTINGS).paused_from is FsmState.S2
    assert (
        build_facts(session(stack=[Frame(state=FsmState.S2)]), None, None, SETTINGS).paused_from
        is None
    )
