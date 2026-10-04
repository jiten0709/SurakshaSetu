"""build_facts: the decide node's input to fsm.transition() (Step 15 §12, the builder's contracts).

Consent, eligibility and suitability come from the Consent Service's record and the engines'
results held in the session; signals come from the turn's intents, after the input rails (a blocked
turn keeps only META_WITHDRAW, I5). The model never sets consent, eligibility, suitability or a
selection. Signals the later steps own (actions, timers, corrections, the S3 selection) stay at
their "nothing happened" defaults until those steps set them.
"""

from typing import Literal

from surakshasetu.analysis.models import Intent, TurnAnalysis
from surakshasetu.config import Settings
from surakshasetu.domain.models import ConsentRecord
from surakshasetu.fsm.facts import (
    ConsentStatus,
    Eligibility,
    Facts,
    MandatoryTrigger,
    S0Intent,
    Suitability,
)
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph.state import SessionState

S0_INTENTS: dict[Intent, S0Intent] = {
    Intent.NEW_PURCHASE: "new_purchase",
    Intent.SPECIFIC_PLAN: "specific_plan",
    Intent.EXISTING_POLICY: "existing_policy",
    Intent.GENERAL_FAQ: "general_faq",
}
OBJECTIONS = frozenset(
    {
        Intent.OBJECTION_PRICE,
        Intent.OBJECTION_TRUST,
        Intent.OBJECTION_COMPETITOR,
        Intent.OBJECTION_GUARANTEE,
        Intent.OBJECTION_OTHER,
    }
)


def consent_status(record: ConsentRecord | None) -> ConsentStatus:
    """none: no record. valid: the service says P1 is valid now. refused: P1 not granted. lapsed:
    anything else that ended it (consent TTL, a superseded notice, a withdrawal, no 18+)."""
    if record is None:
        return "none"
    if record.valid_p1:
        return "valid"
    return "refused" if "P1_NOT_GRANTED" in record.valid_reasons else "lapsed"


def build_facts(
    session: SessionState,
    analysis: TurnAnalysis | None,
    block_reason: Literal["injection", "safety"] | None,
    settings: Settings,
    *,
    action_type: str | None = None,
) -> Facts:
    """action_type: a structured action's type. ERASE (and DELETE /v1/sessions/{id}) is a
    withdrawal that needs no language analysis; HUMAN_REQUEST (an advisor quick reply, Step 19) is
    an explicit request for a person."""
    intents = set(analysis.intents) if analysis else set()
    record = session.consent
    engine = session.eligibility.engine if session.eligibility else None
    suit = session.suitability
    counters = session.counters

    trigger: MandatoryTrigger | None = None
    if block_reason == "safety" or Intent.SAFETY in intents:
        trigger = MandatoryTrigger.SELF_HARM
    elif counters.get("injection", 0) >= settings.injection_hit_limit:
        trigger = MandatoryTrigger.INJECTION_LIMIT

    return Facts(
        consent=consent_status(record),
        intent=next((S0_INTENTS[i] for i in S0_INTENTS if i in intents), None),
        # V2: the 18+ box unticked with P1 granted, or the engine's DATA_ERASURE_EXIT. A stated
        # minor age is a turn signal (S0 and S1 set it before anything is persisted).
        minor=(
            record is not None
            and "AGE_NOT_DECLARED" in record.valid_reasons
            and "P1_NOT_GRANTED" not in record.valid_reasons
        )
        or (engine is not None and engine.outcome == "DATA_ERASURE_EXIT"),
        eligibility=(
            Eligibility(
                outcome=engine.outcome,
                escalation_reason=engine.escalation_reason,
                eligible_uins=frozenset(engine.eligible_uins),
            )
            if engine
            else None
        ),
        express_path=Intent.EXPRESS_PATH in intents,
        suitability=(
            Suitability(
                inputs_sha256=suit.inputs_sha256,
                outcome=suit.outcome,
                escalation_reason=suit.escalation_reason,
                profile_sufficiency=suit.profile_sufficiency,
                affordability=suit.affordability,
            )
            if suit
            else None
        ),
        needs_slots_sha256=session.needs.slots_sha256 if session.needs else None,
        all_rejected=Intent.REJECT_ALL in intents,
        rediscovery_loops=counters.get("rediscovery_loops", 0),
        need_time=Intent.NEED_TIME in intents,
        explicit_decline=Intent.DECLINE in intents,
        paused_from=(
            session.stack[-1].state
            if session.fsm_state is FsmState.PAUSE and session.stack
            else None
        ),
        withdraw=Intent.META_WITHDRAW in intents or action_type == "ERASE",
        human_request=Intent.META_HUMAN in intents or action_type == "HUMAN_REQUEST",
        frustration=Intent.FRUSTRATION in intents,
        low_confidence_streak=counters.get("low_confidence_streak", 0),
        mandatory_trigger=trigger,
        # TurnAnalysis carries no intent confidence (TDD §3.3): the analyser returns only intents
        # it settles on, so GENERAL_FAQ stands for "FAQ intent, confidence >= 0.7".
        faq=Intent.GENERAL_FAQ in intents,
        objection=bool(intents & OBJECTIONS),
    )
