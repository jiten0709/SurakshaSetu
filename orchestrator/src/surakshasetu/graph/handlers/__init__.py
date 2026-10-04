"""The cross-cutting handlers (TDD §3.9, Step 17): data erasure, human escalation, pause and resume,
and safety; and the AI re-disclosure for an identity question (I6, Step 18).

They are reached two ways only. The turn router sends a withdrawal or a safety signal to a
pass-through node that skips the state node. Or decide enters DATA_ERASURE, HUMAN_ESCALATION or
PAUSE through a fsm.transition() row (graph.nodes.after_decide). A state node never branches into
them. Like the state nodes, a handler works on the turn's scratch (runtime.context, a
graph.nodes.Turn), appends its audit events on the turn's transaction, and sets the template parts
compose renders.
"""

from typing import TYPE_CHECKING, Any, cast

from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import EventType, Header
from surakshasetu.compose.bundle import PromptBundle, Scripts
from surakshasetu.graph.state import SessionState
from surakshasetu.store.conv import SessionRow

if TYPE_CHECKING:
    from surakshasetu.graph.nodes import Turn


def session(turn: "Turn") -> SessionState:
    if turn.next is None:
        raise RuntimeError("load has not run")
    return turn.next


def bundle(turn: "Turn") -> PromptBundle:
    return cast(PromptBundle, turn.bundle)


def scripts(turn: "Turn") -> Scripts:
    return bundle(turn).templates[session(turn).locale].scripts


def quick_reply(label: str, action: str, payload: dict[str, Any]) -> dict[str, Any]:
    """One quick reply (Step 18): the label shown, and the structured action it sends."""
    return {"label": label, "action": {"type": action, "payload": payload}}


def append(turn: "Turn", event_type: EventType, header: Header, payload: dict[str, Any]) -> None:
    """One audit event on the turn's chain, in the turn's transaction (committed with the reply)."""
    current = session(turn)
    audit_chain.append(
        turn.conn,
        turn.keys,
        session_id=turn.session_id,
        event_type=event_type,
        fsm_state=current.fsm_state.value,
        pins=current.pins.model_dump(mode="json"),
        header=header,
        payload=payload,
        key_ref=cast(SessionRow, turn.row).key_ref,
    )


def granted(purposes: list[Any]) -> list[str]:
    """P1/P2/P3 for the purposes a consent record grants (P1_NEEDS_RECO -> P1)."""
    return sorted(p.purpose_id[:2] for p in purposes if p.granted)
