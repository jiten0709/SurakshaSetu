"""One positive and one negative case per row id (Step 15), the reason codes Step 17's handlers
read, and PAUSE's resume."""

import pytest
from fsm_support import (
    CASES,
    H2,
    SAVINGS,
    TH,
    Th,
    consented,
    eligibility,
    in_s3,
    ready_to_hand_off,
    suitability,
)

from surakshasetu.fsm.facts import Facts, MandatoryTrigger, Selected
from surakshasetu.fsm.rows import CROSS_CUTTING, STATE_ROWS, Row
from surakshasetu.fsm.states import FsmState
from surakshasetu.fsm.transition import transition

S = FsmState


ESCALATED_NRI = eligibility("HUMAN_ESCALATION", escalation_reason="HE_NRI")
RE_ASK_LIMIT = "HE_RE_ASK_LIMIT"
ROWS: dict[str, Row] = {
    row.id: row for row in (*CROSS_CUTTING, *(r for rows in STATE_ROWS.values() for r in rows))
}


def test_every_row_has_a_case() -> None:
    assert set(CASES) == set(ROWS)


@pytest.mark.parametrize("row_id", sorted(CASES))
def test_the_row_fires_on_its_positive_case(row_id: str) -> None:
    case = CASES[row_id]
    t = transition(case.positive, case.state, TH)

    assert (t.row_id, t.to) == (row_id, case.to)


@pytest.mark.parametrize("row_id", sorted(r for r, c in CASES.items() if c.negative is not None))
def test_the_row_does_not_fire_on_its_negative_case(row_id: str) -> None:
    case = CASES[row_id]
    assert case.negative is not None
    row = ROWS[row_id]

    assert transition(case.negative, case.state, TH).row_id != row_id
    if not row_id.endswith(".STAY"):  # a STAY row always matches; an earlier row preempts it
        assert not row.condition(case.negative, TH)


@pytest.mark.parametrize(
    ("f", "reason"),
    [
        *((consented(mandatory_trigger=m), m.value) for m in MandatoryTrigger),
        (consented(human_request=True), "HE_REQUEST"),
        (consented(frustration=True), "HE_FRUSTRATION"),
        (consented(low_confidence_streak=2), "HE_LOW_CONFIDENCE"),
        # A mandatory trigger names the escalation even when the customer also asked.
        (consented(human_request=True, mandatory_trigger=MandatoryTrigger.SELF_HARM), "HE_SAFETY"),
    ],
)
def test_cross_cutting_escalation_names_its_trigger(f: Facts, reason: str) -> None:
    t = transition(f, S.S2, TH)
    assert (t.row_id, t.reason_code) == ("CC2", reason)


def test_the_low_confidence_streak_limit_is_configuration() -> None:
    f = consented(low_confidence_streak=1)

    assert transition(f, S.S1, TH).to is S.S1
    assert transition(f, S.S1, Th(low_confidence_streak_limit=1)).to is S.HUMAN_ESCALATION


@pytest.mark.parametrize(
    ("state", "f", "reason"),
    [
        (S.S0, Facts(intent="existing_policy"), "HE_EXISTING_POLICY"),
        (
            S.S1,
            consented(eligibility=eligibility("HUMAN_ESCALATION", escalation_reason="HE_NRI")),
            "HE_NRI",
        ),
        (S.S1, consented(eligibility=eligibility("HUMAN_ESCALATION")), "HE_ELIGIBILITY"),
        (
            S.S1,
            consented(eligibility=eligibility("RE_ASK"), reask_exhausted=True),
            "HE_RE_ASK_LIMIT",
        ),
        (S.S2, in_s3(suitability=suitability(outcome="ESCALATE")), "HE_SUITABILITY"),
        (S.S3, in_s3(no_options=True), "HE_NO_OPTION"),
        (S.S3, in_s3(all_rejected=True, rediscovery_loops=2), "HE_REDISCOVERY_LIMIT"),
    ],
)
def test_state_escalations_carry_step_17_reason_codes(
    state: FsmState, f: Facts, reason: str
) -> None:
    t = transition(f, state, TH)
    assert (t.to, t.reason_code) == (S.HUMAN_ESCALATION, reason)


@pytest.mark.parametrize(
    ("f", "to"),
    [
        (
            in_s3(suitability=suitability(profile_sufficiency=0.7)),
            S.S3,
        ),  # the boundary is inclusive
        (in_s3(suitability=suitability(profile_sufficiency=0.6)), S.S2),
        (in_s3(suitability=suitability(profile_sufficiency=0.6), sufficiency_elected=True), S.S3),
        (in_s3(suitability=suitability(affordability="amber"), amber_confirmed=True), S.S3),
        (in_s3(suitability=suitability(affordability="unknown")), S.S3),
        (in_s3(needs_slots_sha256=None), S.S2),
        (in_s3(suitability=None), S.S2),
        (Facts(suitability=suitability(), needs_slots_sha256=suitability().inputs_sha256), S.S0),
    ],
)
def test_the_s3_gate(f: Facts, to: FsmState) -> None:
    assert transition(f, S.S2, TH).to is to


@pytest.mark.parametrize(
    "f",
    [
        ready_to_hand_off(acks_valid_for_selected=False),
        ready_to_hand_off(selected=Selected(uin=SAVINGS, quote_valid=True)),  # not eligible for it
        ready_to_hand_off(eligibility=None),
        ready_to_hand_off(selected=None),
    ],
)
def test_the_hand_off_guard_v7(f: Facts) -> None:
    assert transition(f, S.S3, TH).to is not S.HANDOFF


def test_the_rediscovery_loop_limit_is_configuration() -> None:
    f = in_s3(all_rejected=True, rediscovery_loops=2)

    assert transition(f, S.S3, Th(rediscovery_loop_limit=3)).to is S.S2


@pytest.mark.parametrize(
    ("f", "to", "row_id"),
    [
        (in_s3(paused_from=S.S3), S.S3, "PAUSE.R"),
        (in_s3(paused_from=S.S3, consent="lapsed"), S.S0, "G1"),  # §3.5: re-consent first
        (in_s3(paused_from=S.S3, needs_slots_sha256=H2), S.S2, "G4"),  # I2 on resume
        (in_s3(paused_from=S.S3, faq=True), S.S3, "CC4"),
        (ready_to_hand_off(paused_from=S.S3), S.HANDOFF, "S3.4"),
        (ready_to_hand_off(paused_from=S.QUOTE_ONLY), S.QUOTE_ONLY, "PAUSE.R"),
        (consented(paused_from=None), S.S0, "PAUSE.R"),
        (consented(paused_from=S.EXIT), S.S0, "PAUSE.R"),
        (consented(paused_from=S.S2, withdraw=True), S.DATA_ERASURE, "CC1"),
        (
            consented(paused_from=S.S2, inactivity_timeout=True, mandatory_trigger="HE_SAFETY"),
            S.HUMAN_ESCALATION,
            "CC2",
        ),
    ],
)
def test_a_customer_turn_resumes_into_the_paused_from_state(
    f: Facts, to: FsmState, row_id: str
) -> None:
    t = transition(f, S.PAUSE, TH)
    assert (t.to, t.row_id) == (to, row_id)


@pytest.mark.parametrize(
    ("f", "state", "to", "reason"),
    [
        (consented(inactivity_timeout=True), S.S2, S.PAUSE, "INACTIVITY"),
        (consented(dependency_down=True), S.S2, S.PAUSE, "DEPENDENCY_DOWN"),
        (in_s3(dependency_down=True), S.S3, S.PAUSE, "DEPENDENCY_DOWN"),
        (Facts(dependency_down=True), S.S0, S.S0, "STAY"),  # V3: never before consent
    ],
)
def test_a_dependency_down_pauses_after_consent_like_inactivity(
    f: Facts, state: FsmState, to: FsmState, reason: str
) -> None:
    """TDD §3.9 "Dependency down": S2 pauses, saves and resumes later (Step 20, CC3)."""
    t = transition(f, state, TH)
    assert (t.to, t.reason_code) == (to, reason)


def test_a_resume_reports_resumed() -> None:
    t = transition(consented(paused_from=S.S1), S.PAUSE, TH)
    assert (t.reason_code, t.subgraph) == ("RESUMED", None)


@pytest.mark.parametrize(("row_id", "subgraph"), [("CC4", "side_query"), ("CC5", "objection")])
def test_faq_and_objection_return_to_origin_through_their_subgraph(
    row_id: str, subgraph: str
) -> None:
    case = CASES[row_id]
    t = transition(case.positive, case.state, TH)
    assert (t.to, t.subgraph) == (case.state, subgraph)


def test_the_quote_only_hard_block_names_c13() -> None:
    t = transition(consented(apply_request=True), S.QUOTE_ONLY, TH)
    assert (t.to, t.reason_code) == (S.QUOTE_ONLY, "HARD_BLOCK_C13")


@pytest.mark.parametrize(
    ("state", "invariants"),
    [(S.EXIT, {"I5": False}), (S.HANDOFF, {"I5": False}), (S.DATA_ERASURE, {"I5": True})],
)
def test_a_withdrawal_on_a_closed_session_is_reported_not_honoured(
    state: FsmState, invariants: dict[str, bool]
) -> None:
    # Your decision (Q3): terminal states stay closed; Step 17 routes this to erasure.
    assert transition(Facts(withdraw=True), state, TH).invariants == invariants
