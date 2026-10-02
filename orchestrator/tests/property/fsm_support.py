"""Builders, the row case table, the field domains and the Hypothesis strategies for the fsm tests
(Step 15).

`scenarios` draws (state, Facts) two ways:
- arbitrary: every field drawn independently from its domain, incoherent combinations included;
- near a row: a row's own positive case from CASES with up to three fields redrawn, so every
  row's neighbourhood (its boundaries with the rows around it) is explored.
Arbitrary draws alone almost never reach the rows behind the cross-cutting ones: a scratch count
over 3,000 of them never reached S3's rows, so the hand-off property held only vacuously.
"""

from dataclasses import dataclass
from typing import Any, NamedTuple, get_args

from hypothesis import strategies as st

from surakshasetu.fsm.facts import (
    Affordability,
    ConsentStatus,
    Eligibility,
    EligibilityOutcome,
    Facts,
    MandatoryTrigger,
    S0Intent,
    Selected,
    Suitability,
    SuitabilityOutcome,
)
from surakshasetu.fsm.states import TERMINAL, FsmState

S = FsmState
H1, H2 = "a" * 64, "b" * 64
TERM, ROP, SAVINGS = "999N001V02", "999N002V01", "999N010V01"


@dataclass(frozen=True)
class Th:
    """A Thresholds stand-in; the defaults are the Settings defaults."""

    profile_sufficiency_min: float = 0.7
    rediscovery_loop_limit: int = 2
    low_confidence_streak_limit: int = 2


TH = Th()


def suitability(**overrides: Any) -> Suitability:
    """A FIT record bound to H1: sufficient, green."""
    values: dict[str, Any] = {
        "inputs_sha256": H1,
        "outcome": "FIT",
        "profile_sufficiency": 0.9,
        "affordability": "green",
    }
    return Suitability(**(values | overrides))


def eligibility(outcome: str = "ELIGIBLE", **overrides: Any) -> Eligibility:
    values: dict[str, Any] = {"outcome": outcome, "eligible_uins": frozenset({TERM, ROP})}
    return Eligibility(**(values | overrides))


def consented(**overrides: Any) -> Facts:
    return Facts(**({"consent": "valid"} | overrides))


def in_s3(**overrides: Any) -> Facts:
    """Facts that keep a session in S3: valid consent, eligible, a current FIT record."""
    values: dict[str, Any] = {
        "consent": "valid",
        "eligibility": eligibility(),
        "suitability": suitability(),
        "needs_slots_sha256": H1,
    }
    return Facts(**(values | overrides))


def ready_to_hand_off(**overrides: Any) -> Facts:
    values: dict[str, Any] = {
        "selected": Selected(uin=TERM, quote_valid=True),
        "acks_valid_for_selected": True,
    }
    return in_s3(**(values | overrides))


class Case(NamedTuple):
    state: FsmState
    positive: Facts
    to: FsmState
    negative: Facts | None  # None only for a terminal STAY: nothing can precede it


# One positive and one negative case per row id; test_fsm_rows.py checks both and that every row
# has one.
CASES: dict[str, Case] = {
    # Cross-cutting rows and guards, in evaluation order.
    "CC1": Case(S.S2, consented(withdraw=True), S.DATA_ERASURE, consented()),
    "CC1b": Case(S.S0, Facts(minor=True), S.DATA_ERASURE, Facts()),
    "CC2": Case(S.S1, consented(human_request=True), S.HUMAN_ESCALATION, consented()),
    "CC3": Case(S.S2, consented(inactivity_timeout=True), S.PAUSE, consented()),
    "G1": Case(S.S2, Facts(consent="lapsed"), S.S0, consented()),
    "G2": Case(S.S3, in_s3(correction="eligibility"), S.S1, in_s3(correction="needs")),
    "G3": Case(S.S3, in_s3(correction="needs"), S.S2, in_s3()),
    "G4": Case(S.S3, in_s3(needs_slots_sha256=H2), S.S2, in_s3()),
    "CC4": Case(S.S1, consented(faq=True), S.S1, consented()),
    "CC5": Case(S.S3, in_s3(objection=True), S.S3, in_s3()),
    # S0 (TDD §3.5).
    "S0.1": Case(
        S.S0, Facts(intent="existing_policy"), S.HUMAN_ESCALATION, Facts(intent="general_faq")
    ),
    "S0.2": Case(S.S0, Facts(consent="refused"), S.EXIT, Facts()),
    "S0.3": Case(
        S.S0, consented(intent="specific_plan"), S.QUOTE_ONLY, Facts(intent="specific_plan")
    ),
    "S0.4": Case(S.S0, consented(intent="new_purchase"), S.S1, Facts(intent="new_purchase")),
    "S0.STAY": Case(S.S0, consented(), S.S0, Facts(consent="refused")),
    # S1 (TDD §3.6).
    "S1.0": Case(
        S.S1,
        consented(eligibility=eligibility("DATA_ERASURE_EXIT")),
        S.DATA_ERASURE,
        consented(eligibility=eligibility("RE_ASK")),
    ),
    "S1.1": Case(
        S.S1,
        consented(eligibility=eligibility("HUMAN_ESCALATION", escalation_reason="HE_NRI")),
        S.HUMAN_ESCALATION,
        consented(eligibility=eligibility("NOT_ELIGIBLE")),
    ),
    "S1.1b": Case(
        S.S1,
        consented(eligibility=eligibility("RE_ASK"), reask_exhausted=True),
        S.HUMAN_ESCALATION,
        consented(eligibility=eligibility("RE_ASK")),
    ),
    "S1.2": Case(
        S.S1,
        consented(eligibility=eligibility("NOT_ELIGIBLE")),
        S.EXIT,
        consented(eligibility=eligibility()),
    ),
    "S1.3": Case(
        S.S1,
        consented(eligibility=eligibility(), express_path=True),
        S.QUOTE_ONLY,
        consented(eligibility=eligibility()),
    ),
    "S1.4": Case(S.S1, consented(eligibility=eligibility()), S.S2, consented()),
    "S1.STAY": Case(S.S1, consented(), S.S1, consented(eligibility=eligibility())),
    # Quote-Only (TDD §3.6).
    "QO.1": Case(
        S.QUOTE_ONLY,
        consented(apply_request=True, quote_satisfied=True),
        S.QUOTE_ONLY,
        consented(quote_satisfied=True),
    ),
    "QO.2": Case(S.QUOTE_ONLY, consented(quote_satisfied=True), S.EXIT, consented()),
    "QO.3": Case(
        S.QUOTE_ONLY,
        consented(advisory_opt_in=True, eligibility=eligibility()),
        S.S2,
        consented(advisory_opt_in=True),
    ),
    "QO.3b": Case(
        S.QUOTE_ONLY,
        consented(advisory_opt_in=True),
        S.S1,
        consented(advisory_opt_in=True, eligibility=eligibility()),
    ),
    "QO.STAY": Case(S.QUOTE_ONLY, consented(), S.QUOTE_ONLY, consented(quote_satisfied=True)),
    # S2 (TDD §3.7).
    "S2.1b": Case(
        S.S2,
        in_s3(suitability=suitability(outcome="ESCALATE", escalation_reason="HE_OUT_OF_SCOPE")),
        S.HUMAN_ESCALATION,
        in_s3(suitability=suitability(outcome="ESCALATE"), needs_slots_sha256=H2),
    ),
    "S2.1": Case(
        S.S2,
        in_s3(suitability=suitability(outcome="NO_GAP")),
        S.EXIT_ADVISORY,
        in_s3(suitability=suitability(outcome="NO_GAP"), needs_slots_sha256=H2),
    ),
    "S2.2": Case(S.S2, in_s3(), S.S3, in_s3(suitability=suitability(affordability="amber"))),
    "S2.STAY": Case(S.S2, consented(), S.S2, in_s3()),
    # S3 (TDD §3.8).
    "S3.0": Case(S.S3, in_s3(no_options=True), S.HUMAN_ESCALATION, in_s3()),
    "S3.1": Case(
        S.S3,
        in_s3(explicit_decline=True, rediscovery_loops=1),
        S.EXIT,
        in_s3(explicit_decline=True),
    ),
    "S3.2": Case(S.S3, in_s3(need_time=True), S.PAUSE, in_s3()),
    "S3.3": Case(
        S.S3,
        in_s3(all_rejected=True, rediscovery_loops=1),
        S.S2,
        in_s3(all_rejected=True, rediscovery_loops=2),
    ),
    "S3.3b": Case(
        S.S3,
        in_s3(all_rejected=True, rediscovery_loops=2),
        S.HUMAN_ESCALATION,
        in_s3(all_rejected=True, rediscovery_loops=1),
    ),
    "S3.4": Case(
        S.S3,
        ready_to_hand_off(),
        S.HANDOFF,
        ready_to_hand_off(selected=Selected(uin=TERM, quote_valid=False)),
    ),
    "S3.STAY": Case(S.S3, in_s3(), S.S3, in_s3(need_time=True)),
    # PAUSE: a customer turn resumes; a timer tick stays.
    "PAUSE.R": Case(
        S.PAUSE,
        consented(paused_from=S.S1),
        S.S1,
        consented(paused_from=S.S1, inactivity_timeout=True),
    ),
    "PAUSE.STAY": Case(
        S.PAUSE,
        consented(paused_from=S.S1, inactivity_timeout=True),
        S.PAUSE,
        consented(paused_from=S.S1),
    ),
    # Terminal states: one unconditional STAY row each.
    **{f"{s.value}.STAY": Case(s, Facts(withdraw=True), s, None) for s in sorted(TERMINAL)},
}

BOOL = [False, True]
# A small domain per Facts field: every value a row distinguishes, plus the boundaries. The sweep
# in test_fsm_invariants.py tries each one on every case; the Hypothesis draws pick from them.
DOMAINS: dict[str, list[Any]] = {
    "consent": list(get_args(ConsentStatus)),
    "intent": [None, *get_args(S0Intent)],
    "eligibility": [
        None,
        *(eligibility(outcome) for outcome in get_args(EligibilityOutcome)),
        eligibility("HUMAN_ESCALATION", escalation_reason="HE_NRI"),
        eligibility(eligible_uins=frozenset({ROP})),  # eligible, but not for TERM
    ],
    "suitability": [
        None,
        suitability(),
        suitability(inputs_sha256=H2),
        suitability(outcome="NO_GAP"),
        suitability(outcome="ESCALATE"),
        suitability(outcome="ESCALATE", escalation_reason="HE_VULNERABLE_COMPLEX"),
        suitability(profile_sufficiency=0.69),
        suitability(profile_sufficiency=0.7),
        suitability(affordability="amber"),
        suitability(affordability="red"),
    ],
    "needs_slots_sha256": [None, H1, H2],
    "selected": [
        None,
        Selected(uin=TERM, quote_valid=True),
        Selected(uin=TERM, quote_valid=False),
        Selected(uin=SAVINGS, quote_valid=True),
    ],
    "rediscovery_loops": [0, 1, 2, 3],
    "correction": [None, "eligibility", "needs"],
    "paused_from": [None, *FsmState],
    "low_confidence_streak": [0, 1, 2, 3],
    "mandatory_trigger": [None, *MandatoryTrigger],
    **dict.fromkeys(
        [
            "minor", "reask_exhausted", "express_path", "quote_satisfied", "advisory_opt_in",
            "apply_request", "sufficiency_elected", "amber_confirmed", "acks_valid_for_selected",
            "no_options", "all_rejected", "need_time", "explicit_decline", "withdraw",
            "human_request", "frustration", "inactivity_timeout", "faq", "objection",
        ],
        BOOL,
    ),
}  # fmt: skip
FIELDS: dict[str, st.SearchStrategy[Any]] = {
    name: st.sampled_from(values) for name, values in DOMAINS.items()
} | {
    # Any sufficiency, not only the boundaries.
    "suitability": st.none()
    | st.builds(
        Suitability,
        inputs_sha256=st.sampled_from([H1, H2]),
        outcome=st.sampled_from(get_args(SuitabilityOutcome)),
        profile_sufficiency=st.floats(0, 1),
        affordability=st.sampled_from(get_args(Affordability)),
    ),
}


def _near(case: Case) -> st.SearchStrategy[tuple[FsmState, Facts]]:
    """The case's state with its positive Facts, up to three fields redrawn."""
    redrawn = st.lists(st.sampled_from(sorted(FIELDS)), max_size=3, unique=True).flatmap(
        lambda names: st.fixed_dictionaries({name: FIELDS[name] for name in names})
    )
    return redrawn.map(lambda values: (case.state, case.positive.model_copy(update=values)))


facts = st.builds(Facts, **FIELDS)
scenarios = st.one_of(
    st.tuples(st.sampled_from(FsmState), facts),
    st.sampled_from(list(CASES.values())).flatmap(_near),
)
thresholds = st.builds(
    Th,
    profile_sufficiency_min=st.sampled_from([0.0, 0.69, 0.7, 1.0]) | st.floats(0, 1),
    rediscovery_loop_limit=st.integers(1, 3),
    low_confidence_streak_limit=st.integers(1, 3),
)
