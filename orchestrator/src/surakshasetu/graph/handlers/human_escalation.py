"""Human Escalation (row CC2 and the states' HE rows; TDD §3.9, §7.1).

Entering HUMAN_ESCALATION (`escalate`, after decide), nothing reaches an advisor without the
customer's P2 consent (advisor contact):
- no valid P1 (S0, or consent lapsed), or any escalation from S0 (TDD §3.9's S0 column; an
  existing policy is servicing, not a sales hand-off; Step 18): contact options only, and no data
  is shared;
- P1 but no P2: ask for P2 first, with yes/no quick replies; nothing is shared yet;
- P2 granted: hand off. The redacted advisor briefing goes into a conv.handoff row (queued) and a
  HANDOFF event. The briefing holds the profile slots, state history, reason, recommendation and
  acknowledgments, so the advisor never re-asks.

After that, the session is closed (HE is terminal). Its node (`node`) takes the answer to the P2
question as the ADVISOR_CONTACT action. The Consent Service records the grant, and then the hand-off
goes ahead. A closed advisor queue still queues the row; the customer then gets contact options
instead of the hand-off script.
"""

import logging
from typing import Any, cast
from uuid import UUID

from langgraph.runtime import Runtime

from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import ConsentCapturedHeader, EventType, HandoffHeader
from surakshasetu.domain.models import ConsentRecord, PurposeGrant
from surakshasetu.fsm.states import FsmState
from surakshasetu.fsm.transition import Transition
from surakshasetu.graph.handlers import append, granted, quick_reply, scripts, session
from surakshasetu.graph.state import GraphState
from surakshasetu.rails import redact
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

logger = logging.getLogger(__name__)

ADVISOR_CONTACT = "ADVISOR_CONTACT"  # the action answering the P2 question: {"granted": bool}
# Every reason a row can escalate with (Step 15's codes plus the engines' fallbacks).
REASON_CODES = frozenset(
    {
        "HE_REQUEST",
        "HE_NRI",
        "HE_AGE_BAND",
        "HE_COMPLEX_PROPOSER",
        "HE_EXISTING_POLICY",
        "HE_SAFETY",
        "HE_VULNERABLE_COMPLEX",
        "HE_AFFORDABILITY_RED",
        "HE_OUT_OF_SCOPE",
        "HE_LOW_CONFIDENCE",
        "HE_FRUSTRATION",
        "HE_REDISCOVERY_LIMIT",
        "HE_INJECTION",
        "HE_NO_OPTION",
        "HE_RE_ASK_LIMIT",
        "HE_ELIGIBILITY",
        "HE_SUITABILITY",
        "HE_JOURNEY_DOWN",  # Step 21: the application journey did not take the intake
    }
)
# Distress and vulnerability go to a care queue; everything else to the advisors.
CARE = frozenset({"HE_SAFETY", "HE_VULNERABLE_COMPLEX"})


def queue_for(reason: str) -> str:
    return "care" if reason in CARE else "advisor"


def p2_granted(consent: ConsentRecord) -> bool:
    return any(p.purpose_id == "P2_ADVISOR_CONTACT" and p.granted for p in consent.purposes)


async def escalate(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    reason = cast(Transition, turn.transition).reason_code
    consent = session(turn).consent
    texts = scripts(turn)
    if turn.from_state is FsmState.S0 or consent is None or not consent.valid_p1:
        turn.parts = [("contact_options", texts.contact_options)]
        logger.info("escalation %s: contact options, no data shared", reason)
    elif not p2_granted(consent):
        turn.parts = [("advisor_consent_ask", texts.advisor_consent_ask)]
        turn.quick_replies = [
            quick_reply(texts.advisor_contact["granted"], ADVISOR_CONTACT, {"granted": True}),
            quick_reply(texts.advisor_contact["declined"], ADVISOR_CONTACT, {"granted": False}),
        ]
        logger.info("escalation %s: advisor contact (P2) asked first", reason)
    else:
        hand_off(turn, reason)


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """A turn in the closed HE state. ADVISOR_CONTACT {granted: true} with valid consent records P2,
    then hands off once; anything else gets contact options."""
    turn = runtime.context
    current = session(turn)
    consent = current.consent
    action = turn.action or {}
    granted_now = action.get("payload", {}).get("granted") is True
    wants = action.get("type") == ADVISOR_CONTACT and granted_now
    if not (wants and consent is not None and consent.valid_p1):
        turn.parts = [("contact_options", scripts(turn).contact_options)]
        return
    if not p2_granted(consent):
        record = await turn.domain.change_consent_purpose(
            consent.consent_id, PurposeGrant(purpose_id="P2_ADVISOR_CONTACT", granted=True)
        )
        current.consent = record
        append(
            turn,
            EventType.CONSENT_CAPTURED,
            ConsentCapturedHeader(
                consent_id=record.consent_id,
                notice_version=record.notice_version,
                notice_sha256=record.notice_sha256,
                purposes=cast(Any, granted(record.purposes)),
                method="structured_action",
                language=record.notice_language,
                adult_declared=record.age_18_plus_declared,
                captured_at=record.captured_at,
            ),
            {"purpose": "P2_ADVISOR_CONTACT", "granted": True},
        )
    if store.has_handoff(turn.conn, turn.session_id):
        turn.parts = [_reply(turn)]
        return
    hand_off(turn, _escalation_reason(turn))


def hand_off(turn: Any, reason: str) -> UUID:
    row = cast(SessionRow, turn.row)
    queue = queue_for(reason)
    briefing = build_briefing(turn, reason, queue)
    handoff_id = store.insert_handoff(
        turn.conn,
        turn.keys,
        row.key_ref,
        session_id=turn.session_id,
        reason_code=reason,
        queue=queue,
        payload=briefing,
    )
    append(
        turn,
        EventType.HANDOFF,
        HandoffHeader(handoff_id=handoff_id, reason_code=reason, queue=queue),
        {"briefing": briefing},
    )
    turn.parts = [_reply(turn)]
    logger.info(
        "escalation %s: handed off to %s (queue %s)",
        reason,
        queue,
        "open" if turn.settings.advisor_queue_open else "closed: contact options shown",
    )
    return handoff_id


def _reply(turn: Any) -> tuple[str, str]:
    texts = scripts(turn)
    if turn.settings.advisor_queue_open:
        return "handoff", texts.handoff
    return "contact_options", texts.contact_options


def _escalation_reason(turn: Any) -> str:
    """The reason the session entered HE: its last STATE_TRANSITION into HE from another state."""
    for event in reversed(audit_chain.events(turn.conn, turn.session_id)):
        header = event.header
        if (
            event.event_type == EventType.STATE_TRANSITION
            and header["to_state"] == FsmState.HUMAN_ESCALATION
            and header["from_state"] != FsmState.HUMAN_ESCALATION
        ):
            return str(header.get("reason_code") or "HE_REQUEST")
    return "HE_REQUEST"


def build_briefing(turn: Any, reason: str, queue: str) -> dict[str, Any]:
    """What the advisor needs so they never re-ask. Structured values only, no transcript, and
    every string passes through the redaction rail (never-store entities and contact details come
    out as <TYPE_n> tokens)."""
    current, row = session(turn), cast(SessionRow, turn.row)
    history = [
        {
            "from": e.header["from_state"],
            "to": e.header["to_state"],
            "trigger": e.header["trigger"],
            "reason_code": e.header.get("reason_code"),
            "at": e.occurred_at.isoformat(),
        }
        for e in audit_chain.events(turn.conn, turn.session_id)
        if e.event_type == EventType.STATE_TRANSITION
    ]
    recommendation = current.recommendation
    briefing = {
        "reason_code": reason,
        "queue": queue,
        "session_id": str(turn.session_id),
        "locale": current.locale,
        "channel": row.channel,
        "consent_purposes": granted(current.consent.purposes) if current.consent else [],
        "state_history": history,
        "profile": store.current_slots(turn.conn, turn.keys, row.key_ref, turn.session_id),
        "recommendation": (
            recommendation.model_dump(mode="json", exclude={"acks"}) if recommendation else None
        ),
        "disclosure_acks": (
            [a.model_dump(mode="json") for a in recommendation.acks] if recommendation else []
        ),
    }
    redacted: dict[str, Any] = _redacted(briefing)
    return redacted


def _redacted(value: Any) -> Any:
    if isinstance(value, str):
        return redact.redact(value).redacted
    if isinstance(value, dict):
        return {k: _redacted(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v) for v in value]
    return value
