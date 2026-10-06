"""The transition rows: TDD §3.5-3.9 with V1-V7, and the Step 15 decisions.

Evaluation for a state (V1): the cross-cutting rows whose scope includes it, by order, then the
state's own rows, by order. Each state's table ends with a STAY row that always matches, so exactly
one row decides: the first that matches. Order puts hard blocks first, then escalations, exits,
pauses and loops back, then forward progress; a row whose condition implies another's goes first.

The cross-cutting table is TDD §3.9's five rows plus:
- CC1b, an under-18 signal -> DATA_ERASURE from every active state (V2; decided 2026-10-02);
- CC3 also pauses when a domain dependency is down (TDD §3.9 "Dependency down": S2 pauses, saves
  and resumes later; decided 2026-10-04, Step 20). As for inactivity, only after consent (V3);
- the guards G1-G4 (decided 2026-10-02). They sit before CC4 and CC5 because a FAQ or objection
  STAY would otherwise keep a session in place with lapsed consent (I1) or a stale suitability
  record (I2), and would lose a one-turn correction (V4).
- CC3b and CC5b (decided 2026-10-05, Step 22): a deferral pauses S1, Quote-Only and S2 (S3 has
  S3.2), and the END quick reply after a repeated objection exits, after the guards.
PAUSE runs only CC1, CC1b and CC2 itself. A customer turn resumes (PAUSE.R) and is evaluated in
the paused-from state, guards included; a timer tick stays.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from surakshasetu.fsm.facts import Facts
from surakshasetu.fsm.states import TERMINAL, FsmState

S = FsmState


@runtime_checkable
class Thresholds(Protocol):
    """The rows' configuration (TDD §3.9: thresholds are configuration, never literals or prompt
    text). surakshasetu.config.Settings satisfies it; the fsm cannot import config."""

    @property
    def profile_sufficiency_min(self) -> float: ...

    @property
    def rediscovery_loop_limit(self) -> int: ...

    @property
    def low_confidence_streak_limit(self) -> int: ...


Condition = Callable[[Facts, Thresholds], bool]
# STAY keeps the current state; RESUME re-enters the paused-from state (PAUSE.R only).
Target = FsmState | Literal["STAY", "RESUME"]


@dataclass(frozen=True)
class Row:
    id: str
    states: frozenset[FsmState]  # where the row applies
    order: int
    to: Target
    condition: Condition
    trigger: str  # the TDD's trigger, for the diagram
    reason_code: str | Callable[[Facts], str]
    matrix_priority: str | None = None
    fallback: str | None = None
    subgraph: Literal["side_query", "objection"] | None = None

    def reason(self, facts: Facts) -> str:
        return self.reason_code if isinstance(self.reason_code, str) else self.reason_code(facts)


def _escalation_reason(f: Facts) -> str:
    """CC2's reason, Step 17's codes: a mandatory trigger names itself first."""
    if f.mandatory_trigger is not None:
        return f.mandatory_trigger.value
    if f.human_request:
        return "HE_REQUEST"
    return "HE_FRUSTRATION" if f.frustration else "HE_LOW_CONFIDENCE"


def _pause_reason(f: Facts) -> str:
    return "INACTIVITY" if f.inactivity_timeout else "DEPENDENCY_DOWN"


def _eligibility_reason(f: Facts) -> str:
    reason = f.eligibility.escalation_reason if f.eligibility is not None else None
    return reason or "HE_ELIGIBILITY"


def _suitability_reason(f: Facts) -> str:
    reason = f.suitability.escalation_reason if f.suitability is not None else None
    return reason or "HE_SUITABILITY"


def _outcome(f: Facts) -> str | None:
    return f.eligibility.outcome if f.eligibility is not None else None


def _suitable(f: Facts, t: Thresholds) -> bool:
    """S2 -> S3: a current FIT record (I2), sufficient or elected, amber only once confirmed."""
    s = f.current_suitability
    return (
        s is not None
        and s.outcome == "FIT"
        and (s.profile_sufficiency >= t.profile_sufficiency_min or f.sufficiency_elected)
        and (s.affordability != "amber" or f.amber_confirmed)
    )


def _can_hand_off(f: Facts, t: Thresholds) -> bool:
    """V7: a chosen plan the engine found eligible, its quote valid, every ack hash-bound."""
    chosen, elig = f.selected, f.eligibility
    return (
        chosen is not None
        and chosen.quote_valid
        and f.acks_valid_for_selected
        and elig is not None
        and elig.outcome == "ELIGIBLE"
        and chosen.uin in elig.eligible_uins
    )


def _always(f: Facts, t: Thresholds) -> bool:
    return True


ENGAGED = frozenset({S.S0, S.S1, S.QUOTE_ONLY, S.S2, S.S3})
ACTIVE = ENGAGED | {S.PAUSE}

CROSS_CUTTING: tuple[Row, ...] = (
    Row("CC1", ACTIVE, 1, S.DATA_ERASURE, lambda f, t: f.withdraw,
        "consent withdrawal or erasure request", "WITHDRAW",
        fallback="Erase per TDD §4.4; the legal record is kept (I5)"),
    Row("CC1b", ACTIVE, 2, S.DATA_ERASURE, lambda f, t: f.minor,
        "under-18 signal (V2)", "MINOR",
        fallback="Polite exit; nothing retained; the key is destroyed now"),
    Row("CC2", ACTIVE, 3, S.HUMAN_ESCALATION,
        lambda f, t: (
            f.mandatory_trigger is not None
            or f.human_request
            or f.frustration
            or f.low_confidence_streak >= t.low_confidence_streak_limit
        ),
        "mandatory trigger, explicit request, frustration or low confidence", _escalation_reason),
    Row("CC3", ENGAGED, 4, S.PAUSE,
        lambda f, t: f.consent_given_ever and (f.inactivity_timeout or f.dependency_down),
        "inactivity timeout or a dependency down, after consent (V3)", _pause_reason,
        fallback="Re-engagement within the granted purposes only; resume re-runs the state"),
    Row("G1", frozenset({S.S1, S.QUOTE_ONLY, S.S2, S.S3}), 5, S.S0, lambda f, t: not f.valid_p1,
        "consent lapsed or notice superseded (I1)", "CONSENT_LAPSED",
        fallback="Re-enter S0 (TDD §3.5)"),
    Row("G2", frozenset({S.S2, S.S3}), 6, S.S1, lambda f, t: f.correction == "eligibility",
        "eligibility fact corrected (V4)", "CORRECTION_ELIGIBILITY",
        fallback="Re-run S1's rows: read-back and a new eligibility call"),
    Row("G3", frozenset({S.S3}), 7, S.S2, lambda f, t: f.correction == "needs",
        "needs fact corrected (V4)", "CORRECTION_NEEDS",
        fallback="Re-run S2's rows: a new hash, then a new suitability record"),
    Row("G4", frozenset({S.S3}), 8, S.S2, lambda f, t: not f.suitability_current,
        "suitability not current (I2)", "SUITABILITY_STALE"),
    Row("CC4", ENGAGED, 9, "STAY", lambda f, t: f.faq,
        "FAQ intent, confidence >= 0.7", "FAQ", "P3", "Return to origin", "side_query"),
    Row("CC5", ENGAGED, 10, "STAY", lambda f, t: f.objection,
        "objection intent", "OBJECTION", fallback="Return to origin", subgraph="objection"),
    # Step 22 (decided 2026-10-05): a deferral ("I'll think about it", "need to ask my spouse") or
    # SAVE pauses outside S3 too, after consent (V3); S3 keeps S3.2 and its summary. And the END
    # quick reply offered after a repeated objection exits (TDD §3.9: "offers Pause or Exit").
    Row("CC3b", frozenset({S.S1, S.QUOTE_ONLY, S.S2}), 11, S.PAUSE,
        lambda f, t: f.consent_given_ever and f.need_time,
        "needs time (a deferral), after consent", "NEED_TIME",
        fallback="Save progress; resume re-runs the state"),
    Row("CC5b", frozenset({S.S1, S.QUOTE_ONLY, S.S2, S.S3}), 12, S.EXIT,
        lambda f, t: f.end_requested,
        "ends the conversation after a repeated objection", "OBJECTION_EXIT",
        fallback="Graceful exit; re-engagement only with P3"),
)  # fmt: skip


def _table(state: FsmState, prefix: str, *rows: Row) -> tuple[Row, ...]:
    stay = Row(f"{prefix}.STAY", frozenset({state}), 99, "STAY", _always, "no row matched", "STAY")
    return (*rows, stay)


def _in(state: FsmState) -> frozenset[FsmState]:
    return frozenset({state})


STATE_ROWS: dict[FsmState, tuple[Row, ...]] = {
    S.S0: _table(
        S.S0, "S0",
        Row("S0.1", _in(S.S0), 1, S.HUMAN_ESCALATION, lambda f, t: f.intent == "existing_policy",
            "existing_policy", "HE_EXISTING_POLICY", "P4"),
        Row("S0.2", _in(S.S0), 2, S.EXIT, lambda f, t: f.consent == "refused",
            "consent refused", "CONSENT_REFUSED",
            fallback="Helpline and branch locator; collect nothing"),
        Row("S0.3", _in(S.S0), 3, S.QUOTE_ONLY,
            lambda f, t: f.valid_p1 and f.intent == "specific_plan",
            "consent + specific_plan", "SPECIFIC_PLAN", "P2",
            "Collect age, gender, smoker_status first"),
        Row("S0.4", _in(S.S0), 4, S.S1, lambda f, t: f.valid_p1 and f.intent == "new_purchase",
            "consent + new_purchase", "NEW_PURCHASE", "P1",
            "Clarify intent (max 2 attempts), then default to new_purchase"),
    ),
    S.S1: _table(
        S.S1, "S1",
        Row("S1.0", _in(S.S1), 0, S.DATA_ERASURE, lambda f, t: _outcome(f) == "DATA_ERASURE_EXIT",
            "under 18 (V2)", "MINOR", fallback="Polite exit; nothing retained"),
        Row("S1.1", _in(S.S1), 1, S.HUMAN_ESCALATION,
            lambda f, t: _outcome(f) == "HUMAN_ESCALATION",
            "NRI, age band, complex proposer", _eligibility_reason),
        Row("S1.1b", _in(S.S1), 2, S.HUMAN_ESCALATION,
            lambda f, t: _outcome(f) == "RE_ASK" and f.reask_exhausted,
            "unanswerable after one re-ask", "HE_RE_ASK_LIMIT"),
        Row("S1.2", _in(S.S1), 3, S.EXIT, lambda f, t: _outcome(f) == "NOT_ELIGIBLE",
            "not eligible", "NOT_ELIGIBLE",
            fallback="Explain, suggest alternatives, provide helpline"),
        Row("S1.3", _in(S.S1), 4, S.QUOTE_ONLY, lambda f, t: f.eligible and f.express_path,
            "eligible + express_path", "EXPRESS_PATH", "P2"),
        Row("S1.4", _in(S.S1), 5, S.S2, lambda f, t: f.eligible,
            "eligible", "ELIGIBLE", "P1"),
    ),
    S.QUOTE_ONLY: _table(
        S.QUOTE_ONLY, "QO",
        Row("QO.1", _in(S.QUOTE_ONLY), 1, "STAY", lambda f, t: f.apply_request,
            "request to apply", "HARD_BLOCK_C13",
            fallback="Hard block: suitability required before application intake (C13); offer S2"),
        Row("QO.2", _in(S.QUOTE_ONLY), 2, S.EXIT, lambda f, t: f.quote_satisfied,
            "satisfied with quote", "QUOTE_SATISFIED",
            fallback="Offer summary and re-engagement opt-in"),
        Row("QO.3", _in(S.QUOTE_ONLY), 3, S.S2, lambda f, t: f.advisory_opt_in and f.eligible,
            "opts into advisory", "ADVISORY_OPT_IN", "P1"),
        Row("QO.3b", _in(S.QUOTE_ONLY), 4, S.S1,
            lambda f, t: f.advisory_opt_in and not f.eligible,
            "opts into advisory, not yet screened", "ADVISORY_OPT_IN_SCREEN"),
    ),
    S.S2: _table(
        S.S2, "S2",
        Row("S2.1b", _in(S.S2), 1, S.HUMAN_ESCALATION,
            lambda f, t: f.current_suitability is not None
            and f.current_suitability.outcome == "ESCALATE",
            "red affordability, vulnerable + complex, out of scope (V5)", _suitability_reason),
        Row("S2.1", _in(S.S2), 2, S.EXIT_ADVISORY, lambda f, t: f.over_insured,
            "over-insurance", "NO_GAP", fallback="Portfolio review or escalation"),
        Row("S2.2", _in(S.S2), 3, S.S3, _suitable,
            "sufficiency >= min or elected + suitability current", "SUITABLE", "P1",
            "Below the minimum: state the limitation; proceeding needs a recorded election"),
    ),
    S.S3: _table(
        S.S3, "S3",
        Row("S3.0", _in(S.S3), 0, S.HUMAN_ESCALATION, lambda f, t: f.no_options,
            "no eligible option", "HE_NO_OPTION"),
        Row("S3.1", _in(S.S3), 1, S.EXIT,
            # "post re-discovery": at least one S3 -> S2 loop taken.
            lambda f, t: f.explicit_decline and f.rediscovery_loops >= 1,
            "declines after re-discovery", "DECLINED",
            fallback="Graceful exit with re-engagement opt-in"),
        Row("S3.2", _in(S.S3), 2, S.PAUSE, lambda f, t: f.need_time,
            "needs time", "NEED_TIME", "P3",
            "Send summary, schedule re-engagement within channel rules"),
        Row("S3.3", _in(S.S3), 3, S.S2,
            lambda f, t: f.all_rejected and f.rediscovery_loops < t.rediscovery_loop_limit,
            "all plans rejected", "REDISCOVERY", "P2"),
        Row("S3.3b", _in(S.S3), 4, S.HUMAN_ESCALATION,
            lambda f, t: f.all_rejected and f.rediscovery_loops >= t.rediscovery_loop_limit,
            "rejected after the loop limit", "HE_REDISCOVERY_LIMIT"),
        Row("S3.4", _in(S.S3), 5, S.HANDOFF, _can_hand_off,
            "plan chosen + quote valid + disclosures acked (V7)", "HANDOFF", "P1",
            "Re-present disclosures; require explicit affirmative"),
    ),
    S.PAUSE: _table(
        S.PAUSE, "PAUSE",
        Row("PAUSE.R", _in(S.PAUSE), 1, "RESUME", lambda f, t: not f.inactivity_timeout,
            "customer returns", "RESUMED",
            fallback="Revalidate pins, product status, quote, notice and consent TTL"),
    ),
    **{state: _table(state, state.value) for state in sorted(TERMINAL)},
}  # fmt: skip

_EVALUATION: dict[FsmState, tuple[Row, ...]] = {
    state: (
        *sorted((row for row in CROSS_CUTTING if state in row.states), key=lambda r: r.order),
        *sorted(rows, key=lambda r: r.order),
    )
    for state, rows in STATE_ROWS.items()
}


def evaluation_rows(state: FsmState) -> tuple[Row, ...]:
    """The rows transition() tries for `state`, in order; the last always matches."""
    return _EVALUATION[state]
