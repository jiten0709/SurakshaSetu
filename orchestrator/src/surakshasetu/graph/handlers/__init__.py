"""The cross-cutting handlers (TDD §3.9, Step 17): data erasure, human escalation, pause and resume,
and safety; and the AI re-disclosure for an identity question (I6, Step 18).

They are reached two ways only. The turn router sends a withdrawal or a safety signal to a
pass-through node that skips the state node. Or decide enters DATA_ERASURE, HUMAN_ESCALATION or
PAUSE through a fsm.transition() row (graph.nodes.after_decide). A state node never branches into
them. Like the state nodes, a handler works on the turn's scratch (runtime.context, a
graph.nodes.Turn), appends its audit events on the turn's transaction, and sets the template parts
compose renders.
"""

import dataclasses
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from surakshasetu.analysis.models import Intent
from surakshasetu.analysis.pipeline import PipelineResult
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import EventType, Header
from surakshasetu.compose.bundle import PromptBundle, Scripts, mentions
from surakshasetu.config import Settings
from surakshasetu.crypto.keys import KeyService
from surakshasetu.domain.client import DomainClient
from surakshasetu.gateway import Gateway
from surakshasetu.graph.state import SessionState
from surakshasetu.retrieval.service import RetrievalService
from surakshasetu.store.conv import Conn, SessionRow

if TYPE_CHECKING:
    from surakshasetu.graph.nodes import Turn

Locale = Literal["en-IN", "hi-IN"]


def now() -> datetime:
    """The turn's clock for quote validity and as_of (Step 21). One seam, so the golden harness
    can play a turn "days later"; call it as handlers.now(), never import the name."""
    return datetime.now(UTC)


@dataclasses.dataclass(frozen=True)
class TurnIO:
    """A turn's services and ids without its session (Step 22): what cited generation needs, so
    the side-query subgraph can read a frozen view of the session and never the working copy. Its
    audit events go on the turn's transaction like any other."""

    conn: Conn
    keys: KeyService
    key_ref: str
    session_id: UUID
    out_id: UUID
    gateway: Gateway
    domain: DomainClient
    retrieval: RetrievalService | None
    bundle: PromptBundle
    settings: Settings
    pipeline: PipelineResult | None
    products: dict[str, str]

    def append(
        self, current: SessionState, event_type: EventType, header: Header, payload: dict[str, Any]
    ) -> None:
        audit_chain.append(
            self.conn,
            self.keys,
            session_id=self.session_id,
            event_type=event_type,
            fsm_state=current.fsm_state.value,
            pins=current.pins.model_dump(mode="json"),
            header=header,
            payload=payload,
            key_ref=self.key_ref,
        )


def io(turn: "Turn") -> TurnIO:
    return TurnIO(
        conn=turn.conn,
        keys=turn.keys,
        key_ref=cast(SessionRow, turn.row).key_ref,
        session_id=turn.session_id,
        out_id=cast(UUID, turn.out_id),
        gateway=turn.gateway,
        domain=turn.domain,
        retrieval=turn.retrieval,
        bundle=bundle(turn),
        settings=turn.settings,
        pipeline=turn.pipeline,
        products=turn.products,
    )


def language_request(turn: "Turn", current: SessionState) -> Locale | None:
    """A request to continue in another language (TDD §3.9; Step 22): the LANGUAGE action, the
    bundle's language phrases, or META_LANGUAGE. The phrases name the language; META_LANGUAGE alone
    takes the turn's own language (hi and hi-Latn -> hi-IN), and asking in the language already in
    use means the other one."""
    action = turn.action or {}
    if action.get("type") == "LANGUAGE":
        wanted = (action.get("payload") or {}).get("locale")
        return cast(Locale, wanted) if wanted in ("en-IN", "hi-IN") else None
    pipeline = turn.pipeline
    if pipeline is None or pipeline.blocked:
        return None
    lexicon = bundle(turn).side_query_lexicon
    if mentions(lexicon.language_hi, pipeline.stored_raw):
        return "hi-IN"
    if mentions(lexicon.language_en, pipeline.stored_raw):
        return "en-IN"
    analysis = pipeline.analysis
    if analysis is None or Intent.META_LANGUAGE not in analysis.intents:
        return None
    said: Locale = "en-IN" if analysis.language == "en" else "hi-IN"
    if said == current.locale:
        return "hi-IN" if current.locale == "en-IN" else "en-IN"
    return said


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


# ponytail: catalog names per process; a product added to the catalog needs a restart to be
# recognised by name (by UIN it always is). Load per turn if the catalog starts changing live.
_PRODUCT_NAMES: dict[str, str] = {}


async def product_names(turn: "Turn") -> dict[str, str]:
    """Base product UIN -> catalog name (Step 19): Quote-Only's plan detection, and I3's output
    rail on generated text. Riders are named by UIN only (rails.output)."""
    if not _PRODUCT_NAMES:
        _PRODUCT_NAMES.update({p.uin: p.name for p in await turn.domain.list_products()})
    return dict(_PRODUCT_NAMES)


def granted(purposes: list[Any]) -> list[str]:
    """P1/P2/P3 for the purposes a consent record grants (P1_NEEDS_RECO -> P1)."""
    return sorted(p.purpose_id[:2] for p in purposes if p.granted)
