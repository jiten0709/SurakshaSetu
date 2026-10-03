"""One customer turn as one LangGraph run (TDD §1.4):

load -> input -> route -> <state node> -> decide -> compose -> validate -> commit -> release

The runtime (graph/runtime.py) takes the single-writer lock and the rate limit before invoking the
graph, because a run reads and writes the session's checkpoint even when its first node refuses.

The turn works on a scratch object, `Turn`, passed as the LangGraph runtime context; only `commit`
returns a state update. One app_rw transaction spans the run: analyse_turn (Step 10), decide and
rails.output.release (Step 14) append their audit events as they go, and commit adds the conv rows
and RESPONSE_RELEASED, then commits. Nothing is released before that commit returns (I8), and the
checkpoint is written after it (durability="exit"). The next state comes only from
fsm.transition(), called in decide; edges never decide.
"""

import dataclasses
import hashlib
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command
from redis.exceptions import RedisError

from surakshasetu.analysis.nlu import PendingSlotSpec
from surakshasetu.analysis.pipeline import PipelineResult, TurnContext, analyse_turn
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import (
    EventType,
    ResponseReleasedHeader,
    StateTransitionHeader,
    TurnInputHeader,
)
from surakshasetu.compose.bundle import PromptBundle, load_pinned
from surakshasetu.compose.citations import issue
from surakshasetu.compose.composer import Rendered
from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import canonical_json
from surakshasetu.crypto.keys import KeyService
from surakshasetu.domain.client import DomainClient
from surakshasetu.fsm.states import TERMINAL, FsmState
from surakshasetu.fsm.transition import Transition, transition
from surakshasetu.gateway import Gateway, Route
from surakshasetu.graph import states
from surakshasetu.graph.facts import build_facts
from surakshasetu.graph.gate import RedisGate
from surakshasetu.graph.state import Frame, GraphState, SessionState, VersionPins
from surakshasetu.rails import redact
from surakshasetu.rails.output import LexiconPack, OutputContext, Released, release
from surakshasetu.store import conv as store
from surakshasetu.store.conv import Conn, SessionRow
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger(__name__)

HANDLERS = ("withdraw_consent", "safety")  # cross-cutting handler stubs until Step 17
LANGUAGE = {"en-IN": "en", "hi-IN": "hi"}


@dataclasses.dataclass
class SlotRow:
    """A slot row the state node wants written in the commit (Steps 19-20)."""

    slot: str
    value: Any
    confidence: float
    status: Literal["proposed", "confirmed", "corrected", "declined"]


@dataclasses.dataclass
class Turn:
    """Everything one turn needs and produces. Never checkpointed: it holds the customer's text."""

    settings: Settings
    conn: Conn  # app_rw, one transaction from load to commit
    keys: KeyService
    gateway: Gateway
    domain: DomainClient
    gate: RedisGate
    pack: LexiconPack
    session_id: UUID
    turn_key: UUID
    text: str | None
    action: dict[str, Any] | None
    # load
    row: SessionRow | None = None
    bundle: PromptBundle | None = None
    next: SessionState | None = None  # the working copy commit writes back
    from_state: FsmState = FsmState.S0
    in_id: UUID | None = None
    out_id: UUID | None = None
    in_seq: int = 0
    kill_switches: set[tuple[str, str]] = dataclasses.field(default_factory=set)
    hydrated: bool = False
    replayed: bool = False
    # input, route, state node
    pipeline: PipelineResult | None = None
    routed: str = ""
    slots_pending: list[Any] = dataclasses.field(default_factory=list)  # validated by state nodes
    slot_rows: list[SlotRow] = dataclasses.field(default_factory=list)
    degraded: bool = False
    # decide, compose, validate
    transition: Transition | None = None
    draft: str | None = None
    render: Callable[[str | None], Rendered] | None = None
    regenerate: Callable[[list[str]], Awaitable[str | None]] | None = None
    released: Released | None = None
    # commit
    response: dict[str, Any] | None = None  # set once committed: from then on it is released


def _turn(runtime: Runtime[Turn]) -> Turn:
    return runtime.context


def _session(turn: Turn) -> SessionState:
    if turn.next is None or turn.row is None:
        raise RuntimeError("load has not run")
    return turn.next


async def _publish(turn: Turn, event: str, data: dict[str, Any]) -> None:
    """SSE is best effort: the HTTP response carries the message anyway."""
    try:
        await turn.gate.publish(turn.session_id, event, data)
    except RedisError:
        logger.warning("SSE publish failed for %s", event)


async def _status(turn: Turn, status: str) -> None:
    await _publish(turn, "turn.status", {"turn_id": str(turn.in_id), "status": status})


@lru_cache(maxsize=8)
def pinned_bundle(pinned: str, active: str, kill_switched: bool, env: str) -> PromptBundle:
    """Bundles are hash-locked and immutable, so one load per process is enough."""
    return load_pinned(pinned, active=active, kill_switched=kill_switched, env=env)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- load -----------------------------------------------------------------------------------------
async def load(state: GraphState, runtime: Runtime[Turn]) -> Command[str]:
    turn = _turn(runtime)
    conn, settings = turn.conn, turn.settings
    row = store.get_session(conn, turn.session_id, lock=True)  # LockNotAvailable: another writer
    if row is None:
        raise LookupError("session vanished after authentication")
    turn.row = row

    # Idempotent replay: the conv/audit record is the source; idem:{key} is the fast hint.
    hint = await turn.gate.get_idem(turn.turn_key)
    stored = store.released_response(conn, turn.keys, turn.session_id, turn.turn_key)
    if stored is None and hint is not None:
        raise LookupError("idem key without a committed turn")
    if stored is not None:
        if hint is not None and hint != text_sha256(stored["message"]["text"]):
            logger.error("idempotency hint differs from the committed response; serving the record")
        turn.response, turn.replayed = stored, True
        conn.rollback()
        logger.info("replayed a released turn")
        return Command(goto=END)

    conv_seq = store.last_seq(conn, turn.session_id)
    if state.session is not None and state.committed_seq == conv_seq:
        session = SessionState.model_validate(state.session)
    else:
        session = await _hydrate(turn, row)
        turn.hydrated = True
        if state.session is None and conv_seq == 0:
            logger.debug("first turn: state from conv.session")
        else:
            logger.warning(
                "checkpoint lags conv (checkpoint seq %d, conv seq %d): hydrated from conv",
                state.committed_seq,
                conv_seq,
            )

    turn.kill_switches = store.active_kill_switches(conn)
    pinned = session.pins.prompt_bundle
    switched = ("prompt_bundle", pinned) in turn.kill_switches
    turn.bundle = pinned_bundle(pinned, settings.prompt_bundle, switched, settings.env)
    if turn.bundle.version != pinned:  # I7's exception: a kill switch on the pinned bundle
        logger.info("prompt bundle %s kill-switched: re-pinned to %s", pinned, turn.bundle.version)
        session.pins = session.pins.model_copy(update={"prompt_bundle": turn.bundle.version})

    turn.next, turn.from_state = session, session.fsm_state
    turn.in_id, turn.out_id = uuid7(), uuid7()
    turn.in_seq = conv_seq + 1
    return Command(goto="input")


async def _hydrate(turn: Turn, row: SessionRow) -> SessionState:
    """SessionState from the legal record. Engine results are not stored in conv: they are
    recomputed from the confirmed slots under the pinned rules when a state needs them."""
    consent = await turn.domain.get_consent_record(row.consent_id) if row.consent_id else None
    return SessionState(
        session_id=row.session_id,
        subject_ref=str(row.subject_ref),
        fsm_state=FsmState(row.fsm_state),
        stack=[Frame.model_validate(f) for f in row.frame_stack],
        consent=consent,
        counters=row.counters,
        locale=row.locale,
        pins=VersionPins.model_validate(row.pins),
    )


# --- input and route ------------------------------------------------------------------------------
async def input_node(state: GraphState, runtime: Runtime[Turn]) -> None:
    turn = _turn(runtime)
    session, row = _session(turn), cast(SessionRow, turn.row)
    await _status(turn, "analysing")
    pins = session.pins.model_dump(mode="json")
    if turn.text is None:  # a structured action: no free text, so no input rails
        audit_chain.append(
            turn.conn,
            turn.keys,
            session_id=turn.session_id,
            event_type=EventType.TURN_INPUT,
            fsm_state=session.fsm_state.value,
            pins=pins,
            header=TurnInputHeader(
                turn_id=cast(UUID, turn.in_id),
                turn_seq=turn.in_seq,
                language=LANGUAGE[session.locale],
                channel=cast(Literal["web", "app"], row.channel),
                turn_key=turn.turn_key,
            ),
            payload={"action": turn.action},
            key_ref=row.key_ref,
        )
        return
    turn.pipeline = result = await analyse_turn(
        turn.conn,
        turn.keys,
        turn.gateway,
        turn.text,
        TurnContext(
            session_id=turn.session_id,
            turn_id=cast(UUID, turn.in_id),
            turn_seq=turn.in_seq,
            turn_key=turn.turn_key,
            fsm_state=session.fsm_state.value,
            pins=pins,
            channel=cast(Literal["web", "app"], row.channel),
            key_ref=row.key_ref,
            pending=PendingSlotSpec(session.pending_slot),
        ),
        settings=turn.settings,
    )
    counters = dict(session.counters)
    if result.block_reason == "injection":  # mirrors analyse_turn's UPDATE of conv.session
        counters["injection"] = counters.get("injection", 0) + 1
    floor = turn.settings.confidence_readback_floor
    low = result.analysis is not None and any(s.confidence < floor for s in result.analysis.slots)
    counters["low_confidence_streak"] = counters.get("low_confidence_streak", 0) + 1 if low else 0
    session.counters = counters


def turn_router(
    session: SessionState, pipeline: PipelineResult | None, max_stack: int
) -> tuple[str, Frame | None]:
    """TDD §2.6: a withdrawal first (I5), then safety, then a side query pushes a frame."""
    analysis = pipeline.analysis if pipeline else None
    intents = analysis.intents if analysis else []
    if "META_WITHDRAW" in intents:
        return "withdraw_consent", None
    if "SAFETY" in intents or (pipeline is not None and pipeline.block_reason == "safety"):
        return "safety", None
    if analysis is not None and analysis.side_query and len(session.stack) < max_stack:
        frame = Frame(
            state=session.fsm_state,
            pending_slot=session.pending_slot,
            prompt_id=session.last_prompt_id,
            focus_uins=session.focus_uins,
        )
        return "side_query", frame
    return session.fsm_state.value, None


async def route(state: GraphState, runtime: Runtime[Turn]) -> Command[str]:
    turn = _turn(runtime)
    session = _session(turn)
    if turn.pipeline is not None and turn.pipeline.analysis is not None:
        turn.slots_pending = list(turn.pipeline.analysis.slots)
    goto, frame = turn_router(session, turn.pipeline, turn.settings.side_query_max_stack)
    if frame is not None:
        session.stack = [*session.stack, frame]
    turn.routed = goto
    return Command(goto=goto)


async def withdraw_consent(state: GraphState, runtime: Runtime[Turn]) -> None:
    """Step 17: the erasure handler. decide still routes the withdrawal to DATA_ERASURE (CC1)."""


async def safety(state: GraphState, runtime: Runtime[Turn]) -> None:
    """Step 17: the safety handler. compose already answers with the crisis template."""


async def side_query(state: GraphState, runtime: Runtime[Turn]) -> Command[str]:
    """Step 22: the FAQ subgraph answers, pops its frame and re-asks the pending question. The
    stub pops at once and lets the state node apply any slot in the same turn."""
    session = _session(_turn(runtime))
    frame = session.stack[-1]
    session.stack = session.stack[:-1]
    return Command(goto=frame.state.value)


# --- decide ---------------------------------------------------------------------------------------
async def decide(state: GraphState, runtime: Runtime[Turn]) -> None:
    turn = _turn(runtime)
    session, row = _session(turn), cast(SessionRow, turn.row)
    pipeline = turn.pipeline
    facts = build_facts(
        session,
        pipeline.analysis if pipeline else None,
        pipeline.block_reason if pipeline else None,
        turn.settings,
    )
    before = session.fsm_state
    result = transition(facts, before, turn.settings)
    logger.info(
        "transition %s -> %s by %s (%s)", before, result.to, result.row_id, result.reason_code
    )
    audit_chain.append(
        turn.conn,
        turn.keys,
        session_id=turn.session_id,
        event_type=EventType.STATE_TRANSITION,
        fsm_state=before.value,
        pins=session.pins.model_dump(mode="json"),
        header=StateTransitionHeader(
            from_state=before.value,
            to_state=result.to.value,
            trigger=result.row_id,
            invariants=result.invariants,
            reason_code=result.reason_code,
        ),
        payload={},
        key_ref=row.key_ref,
    )
    if result.row_id == "S3.3":
        session.counters = {
            **session.counters,
            "rediscovery_loops": session.counters.get("rediscovery_loops", 0) + 1,
        }
    if result.to is FsmState.PAUSE and before is not FsmState.PAUSE:
        session.stack = [*session.stack, Frame(state=before, pending_slot=session.pending_slot)]
    elif before is FsmState.PAUSE and result.to is not FsmState.PAUSE and session.stack:
        session.stack = session.stack[:-1]
    session.fsm_state = result.to
    turn.transition = result


# --- compose and validate -------------------------------------------------------------------------
def template(template_id: str, text: str) -> Rendered:
    return Rendered(text, text_sha256(text), [(f"template:{template_id}", text)], {}, [], {}, {})


async def compose(state: GraphState, runtime: Runtime[Turn]) -> None:
    """Template-only until the state steps add cited generation (they set draft, regenerate and
    render, and append MODEL_CALL)."""
    turn = _turn(runtime)
    session = _session(turn)
    await _status(turn, "composing")
    scripts = cast(PromptBundle, turn.bundle).templates[session.locale].scripts
    if turn.pipeline is not None and turn.pipeline.overlong:
        chosen = ("ask_to_shorten", scripts.ask_to_shorten)
    elif turn.routed == "safety":
        chosen = ("safety", scripts.safety)
    elif turn.degraded:
        chosen = ("release_blocked", scripts.release_blocked)
    else:
        chosen = ("advisor_offer", scripts.advisor_offer)
    turn.draft = None
    turn.render = lambda _narrative: template(*chosen)


async def _no_regeneration(errors: list[str]) -> str | None:
    return None


async def validate(state: GraphState, runtime: Runtime[Turn]) -> None:
    turn = _turn(runtime)
    session, row = _session(turn), cast(SessionRow, turn.row)
    await _status(turn, "validating")
    context = OutputContext(
        session_id=turn.session_id,
        turn_id=cast(UUID, turn.out_id),
        subject_ref=row.subject_ref,
        fsm_state=session.fsm_state.value,
        pins=session.pins.model_dump(mode="json"),
        key_ref=row.key_ref,
        locale=session.locale,
        route=Route.GEN_RECOMMEND if session.fsm_state is FsmState.S3 else Route.GEN_CONVERSE,
        handles=issue([], []),
        customer_text=turn.pipeline.stored_raw if turn.pipeline else "",
    )
    turn.released = await release(
        turn.conn,
        turn.keys,
        turn.gateway,
        context,
        pack=turn.pack,
        settings=turn.settings,
        bundle=cast(PromptBundle, turn.bundle),
        draft=turn.draft,
        regenerate=turn.regenerate or _no_regeneration,
        render=cast(Callable[[str | None], Rendered], turn.render),
    )


# --- commit and release ---------------------------------------------------------------------------
def response_body(turn: Turn) -> dict[str, Any]:
    """The Conversation API's turn response; also the RESPONSE_RELEASED payload, so a replay
    returns the same bytes (canonical JSON)."""
    session, released = _session(turn), cast(Released, turn.released)
    rendered = released.rendered
    return {
        "turn_id": str(turn.in_id),
        "state": session.fsm_state.value,
        "message": {
            "text": released.text,
            "parts": [{"id": i, "text": t} for i, t in rendered.parts] if rendered else [],
            "citations": [{"handle": h, "ref": r} for h, r in rendered.citations.items()]
            if rendered
            else [],
            "sources": [
                {
                    "title": s.title,
                    "section": s.section,
                    "version": s.version,
                    "effective_from": s.effective_from.isoformat(),
                    "uri": s.uri,
                }
                for s in rendered.sources
            ]
            if rendered
            else [],
            "disclosures": [
                {"uin": uin, "set_sha256": h} for uin, h in rendered.disclosure_hashes.items()
            ]
            if rendered
            else [],
            "cta": None,
            "quick_replies": [],
        },
        "documents": [
            {"uin": uin, "documents": docs} for uin, docs in rendered.documents_shown.items()
        ]
        if rendered
        else [],
    }


def session_status(state: FsmState) -> str:
    if state is FsmState.PAUSE:
        return "paused"
    return "ended" if state in TERMINAL else "active"


async def commit(state: GraphState, runtime: Runtime[Turn]) -> dict[str, Any]:
    turn = _turn(runtime)
    session, row = _session(turn), cast(SessionRow, turn.row)
    released, pipeline, conn = cast(Released, turn.released), turn.pipeline, turn.conn
    body = response_body(turn)
    in_id, out_id = cast(UUID, turn.in_id), cast(UUID, turn.out_id)
    language = LANGUAGE[session.locale]

    store.insert_turn(
        conn,
        turn.keys,
        row.key_ref,
        turn_id=in_id,
        session_id=turn.session_id,
        seq=turn.in_seq,
        direction="in",
        text=pipeline.stored_raw if pipeline else canonical_json(turn.action).decode(),
        redacted=pipeline.redacted if pipeline else f"[action:{(turn.action or {}).get('type')}]",
        language=pipeline.language if pipeline else language,
        analysis=store.analysis_projection(pipeline.analysis if pipeline else None),
        turn_key=turn.turn_key,
    )
    store.insert_turn(
        conn,
        turn.keys,
        row.key_ref,
        turn_id=out_id,
        session_id=turn.session_id,
        seq=turn.in_seq + 1,
        direction="out",
        text=released.text,
        redacted=redact.redact(released.text).redacted,
        language=language,
        analysis=None,
        turn_key=uuid5(turn.turn_key, "out"),  # UNIQUE (session_id, turn_key)
    )
    consent_id = session.consent.consent_id if session.consent else None
    for slot in turn.slot_rows:
        if consent_id is None:
            raise RuntimeError("a slot row without consent (I1)")
        store.insert_slot(
            conn,
            turn.keys,
            row.key_ref,
            session_id=turn.session_id,
            slot=slot.slot,
            value=slot.value,
            confidence=slot.confidence,
            status=slot.status,
            source_turn=in_id,
            consent_id=consent_id,
        )
    store.update_session(
        conn,
        turn.session_id,
        fsm_state=session.fsm_state.value,
        frame_stack=[f.model_dump(mode="json") for f in session.stack],
        counters=session.counters,
        pins=session.pins.model_dump(mode="json"),
        locale=session.locale,
        status=session_status(session.fsm_state),
        consent_id=consent_id,
    )
    rendered = released.rendered
    audit_chain.append(
        conn,
        turn.keys,
        session_id=turn.session_id,
        event_type=EventType.RESPONSE_RELEASED,
        fsm_state=session.fsm_state.value,
        pins=session.pins.model_dump(mode="json"),
        header=ResponseReleasedHeader(
            turn_id=out_id,
            rendered_sha256=text_sha256(released.text),
            citations=list(rendered.citations) if rendered else [],
            verdicts=released.verdicts,
            disclosure_set_sha256s=sorted(rendered.disclosure_hashes.values()) if rendered else [],
        ),
        payload={"response": body},
        key_ref=row.key_ref,
    )
    conn.commit()
    turn.response = body  # committed: from here on the turn is released, whatever follows
    return {"session": session.model_dump(mode="json"), "committed_seq": turn.in_seq + 1}


async def release_node(state: GraphState, runtime: Runtime[Turn]) -> None:
    turn = _turn(runtime)
    body = cast(dict[str, Any], turn.response)
    try:
        await turn.gate.set_idem(turn.turn_key, text_sha256(body["message"]["text"]))
    except RedisError:
        logger.warning("idempotency hint not stored; a retry replays from conv")
    await _publish(turn, "turn.released", body)
    result = cast(Transition, turn.transition)
    logger.info("turn released: %s -> %s (%s)", turn.from_state, result.to, result.row_id)


# --- the graph ------------------------------------------------------------------------------------
def build_graph(checkpointer: BaseCheckpointSaver[Any] | None) -> CompiledStateGraph[Any, Any]:
    graph = StateGraph(GraphState, context_schema=Turn)
    state_names = tuple(s.value for s in FsmState)
    graph.add_node("load", load, destinations=("input", END))
    graph.add_node("input", input_node)
    graph.add_node("route", route, destinations=(*HANDLERS, "side_query", *state_names))
    graph.add_node("withdraw_consent", withdraw_consent)
    graph.add_node("safety", safety)
    graph.add_node("side_query", side_query, destinations=state_names)
    for fsm_state, node in states.NODES.items():
        graph.add_node(fsm_state.value, states.guarded(fsm_state.value, node))
        graph.add_edge(fsm_state.value, "decide")
    graph.add_node("decide", decide)
    graph.add_node("compose", compose)
    graph.add_node("validate", validate)
    graph.add_node("commit", commit)
    graph.add_node("release", release_node)
    graph.add_edge(START, "load")
    graph.add_edge("input", "route")
    for handler in HANDLERS:
        graph.add_edge(handler, "decide")
    graph.add_edge("decide", "compose")
    graph.add_edge("compose", "validate")
    graph.add_edge("validate", "commit")
    graph.add_edge("commit", "release")
    graph.add_edge("release", END)
    return graph.compile(checkpointer=checkpointer)
