"""Turn analysis (TDD §1.4 stage 3): the nlu-extract call. GatewayUnavailable is not caught here --
it propagates to the caller (analysis.pipeline), which falls back to a template clarifying
question.
"""

import logging
from dataclasses import dataclass
from uuid import UUID

from surakshasetu.analysis.models import TurnAnalysis
from surakshasetu.gateway import DataClass, Gateway, GatewayUnavailable, Route

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PendingSlotSpec:
    """What the FSM currently wants filled. A later step's real FSM supplies this for real; Step
    10 just needs a typed carrier for the L1 pending-slot spec."""

    pending_slot: str | None
    known_slots: tuple[str, ...] = ()


async def extract(
    gateway: Gateway,
    *,
    text: str,
    pending: PendingSlotSpec,
    session_id: UUID,
    turn_id: UUID,
    fsm_state: str,
) -> TurnAnalysis:
    messages = [
        {"role": "system", "content": _system_prompt(pending)},
        {"role": "user", "content": text},
    ]
    result = await gateway.call(
        Route.NLU_EXTRACT,
        data_class=DataClass.SELF_HOSTED_RAW,
        messages=messages,
        session_id=session_id,
        turn_id=turn_id,
        fsm_state=fsm_state,
        response_format=TurnAnalysis,
    )
    analysis = result.parsed
    if analysis is None:
        # response_format was given, so Gateway.call always parses or raises; this is unreachable.
        raise GatewayUnavailable("MALFORMED_RESPONSE")
    valid_slots = [s for s in analysis.slots if s.evidence_span in text]
    dropped = len(analysis.slots) - len(valid_slots)
    if dropped:
        logger.debug("nlu dropped %d slot candidate(s) failing the evidence-span check", dropped)
    return analysis.model_copy(update={"slots": valid_slots})


def _system_prompt(pending: PendingSlotSpec) -> str:
    parts = ["Extract intents and slot candidates from the customer's message."]
    if pending.pending_slot:
        parts.append(f"The assistant just asked for: {pending.pending_slot}.")
    if pending.known_slots:
        parts.append(f"Known slot names: {', '.join(pending.known_slots)}.")
    return " ".join(parts)
