"""One customer turn as one LangGraph run (TDD §1.4):

load -> input -> route -> <state node | handler | side_query | objection | timer> -> decide
-> [handler] -> compose -> validate -> commit -> release

Step 22: a side question runs the side-query subgraph (graph/side_query.py) and an objection the
objection handler (graph/handlers/objection.py); each runs the origin state's node first (it takes
what the turn answered and puts its prompt again), then answers ahead of that prompt. A timer turn
(jobs/timers.py) carries no customer input: it only reports the inactivity to decide (CC3).

The runtime (graph/runtime.py) takes the single-writer lock and the rate limit before invoking the
graph, because a run reads and writes the session's checkpoint even when its first node refuses.

The turn works on a scratch object, `Turn`, passed as the LangGraph runtime context; only `commit`
returns a state update. One app_rw transaction spans the run: analyse_turn (Step 10), decide and
rails.output.release (Step 14) append their audit events as they go, and commit adds the conv rows
and RESPONSE_RELEASED, then commits. Nothing is released before that commit returns (I8), and the
checkpoint is written after it (durability="exit"). The next state comes only from
fsm.transition(), called in decide; edges never decide. The cross-cutting handlers (graph/handlers/)
run where the router or the transition sends them: a withdrawal or a safety signal skips the state
node, and entering DATA_ERASURE, HUMAN_ESCALATION or PAUSE runs that handler before compose.
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

from surakshasetu.analysis.models import Intent
from surakshasetu.analysis.nlu import PendingSlotSpec
from surakshasetu.analysis.pipeline import PipelineResult, TurnContext, analyse_turn
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import (
    EventType,
    ResponseReleasedHeader,
    StateTransitionHeader,
    TurnInputHeader,
)
from surakshasetu.compose.bundle import L1Name, PromptBundle, load_pinned, mentions
from surakshasetu.compose.citations import TurnHandles, issue
from surakshasetu.compose.composer import Rendered
from surakshasetu.compose.envelope import EnvelopeError, SessionFacts, model_call_event
from surakshasetu.compose.envelope import build as build_envelope
from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import canonical_json
from surakshasetu.crypto.keys import KeyService
from surakshasetu.domain.client import DomainClient
from surakshasetu.domain.models import DisclosureSet
from surakshasetu.fsm.facts import Facts
from surakshasetu.fsm.states import TERMINAL, FsmState
from surakshasetu.fsm.transition import Transition, transition
from surakshasetu.gateway import Gateway, GatewayUnavailable, Route
from surakshasetu.graph import handlers as handler_tools
from surakshasetu.graph import side_query as side_query_subgraph
from surakshasetu.graph import states
from surakshasetu.graph.facts import build_facts
from surakshasetu.graph.gate import RedisGate
from surakshasetu.graph.handlers import (
    append,
    data_erasure,
    human_escalation,
    identity,
    objection,
    pause,
    product_names,
    safety,
)
from surakshasetu.graph.state import (
    Frame,
    GraphState,
    RecommendationPayload,
    SessionState,
    Shown,
    SlotRow,
    VersionPins,
)
from surakshasetu.graph.states import quote_only, s0, s1, s2, s3
from surakshasetu.rails import redact
from surakshasetu.rails.output import LexiconPack, Numbers, OutputContext, Released, release
from surakshasetu.retrieval.service import RetrievalService
from surakshasetu.store import conv as store
from surakshasetu.store.conv import Conn, SessionRow
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger(__name__)

# The turn router's pass-through handlers (they skip the state node), and the handlers decide
# routes to when the transition enters their state.
ROUTED = {"withdraw_consent": data_erasure.withdraw_consent, "safety": safety.node}
ENTERED = {
    "data_erasure": data_erasure.node,
    "human_escalation": human_escalation.escalate,
    "pause": pause.pause,
    "s0_enter": s0.enter,  # Step 18: G1 or a resume re-entered S0
    "s1_enter": s1.enter,  # Step 19: S0.4, QO.3b, G2 (V4) or a resume entered S1
    "quote_only_enter": quote_only.enter,  # Step 19: S0.3 or S1.3 entered Quote-Only
    "s2_enter": s2.enter,  # Step 20: S1.4, QO.3, G3 (V4), S3.3 or a resume entered S2
    "s3_enter": s3.enter,  # Step 21: S2.2 or a resume entered S3: the recommendation
}
ENTER = {
    FsmState.S0: "s0_enter",
    FsmState.S1: "s1_enter",
    FsmState.QUOTE_ONLY: "quote_only_enter",
    FsmState.S2: "s2_enter",
    FsmState.S3: "s3_enter",
}
LANGUAGE: dict[str, Literal["en", "hi"]] = {"en-IN": "en", "hi-IN": "hi"}
TIMER = "INACTIVITY"  # a timer turn's input (Step 22): no customer text, no action


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
    safety: bool = False  # a self-harm signal: the crisis script leads the reply
    # handlers: the template parts to send (id, text), and an erasure to run after the commit
    parts: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    erasure: data_erasure.Reason | None = None
    # Step 18. Facts fields a state node sets from what it verified (a structured action, a closed
    # lexicon match); decide validates them into Facts. An identity question (I6). The consent form
    # and quick replies sent with the message. Approved text shown verbatim (the notice and the AI
    # disclosure), which RC-LEAK accepts. Slot names volunteered in S0: memory only, never stored.
    signals: dict[str, Any] = dataclasses.field(default_factory=dict)
    identity: bool = False
    ai_disclosure: str | None = None
    form: dict[str, Any] | None = None
    quick_replies: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    shown: list[str] = dataclasses.field(default_factory=list)
    volunteered: list[str] = dataclasses.field(default_factory=list)
    # Step 19. A plain question compose may lead with one generated sentence: (L1, the slot
    # template's reason-line id, the answered slot names). Catalog names (UIN -> name), for
    # Quote-Only's plan detection and I3's output rail.
    phrase: tuple[L1Name, str, tuple[str, ...]] | None = None
    products: dict[str, str] = dataclasses.field(default_factory=dict)
    # Step 21. The retrieval service (None: no evidence, as in the unit tests). A state that
    # composes its own reply (S3: the recommendation, a cited answer) sets draft/render/regenerate
    # and what the output rails check it against: the handles the model saw, the engine values the
    # placeholders fill from, the disclosure sets shown. The recommendation the commit records.
    retrieval: RetrievalService | None = None
    handles: TurnHandles | None = None
    numbers: Numbers | None = None
    disclosure_sets: dict[str, DisclosureSet] = dataclasses.field(default_factory=dict)
    recommendation: RecommendationPayload | None = None
    # Step 22. The side-query subgraph: the frame the router pushed (None with a full stack), the
    # question, a full stack, its outcome for the RESPONSE_RELEASED header, a state that answered
    # the question itself, and a cited answer every claim of which is verified (gen-recommend).
    # The objection handler's type and response. A timer turn (no customer input). A language
    # switch this turn. The request not to share never-store data.
    frame: Frame | None = None
    side_question: str | None = None
    side_full: bool = False
    side_outcome: str | None = None
    answered: bool = False
    verify_all: bool = False
    objection: str | None = None
    objection_response: str | None = None
    timer: bool = False
    language_switched: bool = False
    reminder: bool = False
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
    if turn.text is None:  # a structured action or a timer: no free text, so no input rails
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
            payload={"timer": TIMER} if turn.timer else {"action": turn.action},
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
            pending=PendingSlotSpec(
                session.pending_slot, known_slots=states.KNOWN_SLOTS.get(session.fsm_state, ())
            ),
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
    session: SessionState,
    pipeline: PipelineResult | None,
    max_stack: int,
    action: dict[str, Any] | None = None,
    *,
    question: str | None = None,
    objecting: bool = False,
) -> tuple[str, Frame | None]:
    """TDD §2.6: a withdrawal first (I5; free text, or the ERASE action), then safety, then (Step
    22) an objection, then a side query, which pushes a frame while the stack has room. With the
    stack full the subgraph still answers, briefly (the abstain line), and returns to the pending
    prompt. `question` is the side question the router found beyond analysis.side_query."""
    analysis = pipeline.analysis if pipeline else None
    intents = analysis.intents if analysis else []
    if "META_WITHDRAW" in intents or (action or {}).get("type") == data_erasure.ERASE:
        return "withdraw_consent", None
    if safety.signal(pipeline):
        return "safety", None
    if objecting:
        return "objection", None
    if question is not None or (analysis is not None and analysis.side_query):
        if len(session.stack) >= max_stack:
            return "side_query", None
        frame = Frame(
            state=session.fsm_state,
            pending_slot=session.pending_slot,
            prompt_id=session.last_prompt_id,
            focus_uins=session.focus_uins,
        )
        return "side_query", frame
    return session.fsm_state.value, None


def side_question(turn: Turn, session: SessionState) -> str | None:
    """A question for the side-query subgraph (Step 22): nlu-extract's side_query; or, read from the
    turn, a FAQ intent anywhere, a question after the conversation has ended or in S3 (where S3's
    own questions are answered from the evidence), an exclusion dispute in S3, regulatory pushback
    in S0, S3 and a closed session (S1 and S2 explain their own question's reason); the FACT
    action answers a proposed fact."""
    if (turn.action or {}).get("type") == "FACT":
        return ""
    pipeline = turn.pipeline
    if pipeline is None or pipeline.blocked:
        return None
    analysis = pipeline.analysis
    if analysis is not None and analysis.side_query:
        return str(analysis.side_query)
    text = pipeline.stored_raw
    state = session.fsm_state
    lexicons = cast(PromptBundle, turn.bundle)
    asked = (
        (analysis is not None and Intent.GENERAL_FAQ in analysis.intents)
        or (state in (FsmState.S3, *TERMINAL) and "?" in text)
        or (state is FsmState.S3 and mentions(lexicons.s3_lexicon.exclusion_dispute, text))
        or (
            state in (FsmState.S0, FsmState.S3, *TERMINAL)
            and mentions(lexicons.side_query_lexicon.pushback, text)
        )
    )
    return text if asked else None


async def route(state: GraphState, runtime: Runtime[Turn]) -> Command[str]:
    turn = _turn(runtime)
    session = _session(turn)
    if turn.timer:  # Step 22: no customer input, only the inactivity (CC3)
        turn.routed = "timer"
        return Command(goto="timer")
    if turn.pipeline is not None and turn.pipeline.analysis is not None:
        turn.slots_pending = list(turn.pipeline.analysis.slots)
    turn.safety = safety.signal(turn.pipeline)
    turn.identity = identity.asks(cast(PromptBundle, turn.bundle), turn.pipeline)
    _switch_language(turn, session)
    turn.reminder = _reminder(turn, session)
    kind = objection.detect(turn)
    question = side_question(turn, session)
    goto, frame = turn_router(
        session,
        turn.pipeline,
        turn.settings.side_query_max_stack,
        turn.action,
        question=question,
        objecting=kind is not None,
    )
    if goto == "objection":
        turn.objection = kind
    if goto == "side_query":
        turn.side_question, turn.side_full = question or "", frame is None
    elif session.counters.get(side_query_subgraph.COUNTER):  # side queries in a row, no more
        session.counters = {**session.counters, side_query_subgraph.COUNTER: 0}
    if frame is not None:
        session.stack = [*session.stack, frame]
        turn.frame = frame
    turn.routed = goto
    return Command(goto=goto)


def _switch_language(turn: Turn, session: SessionState) -> None:
    """TDD §3.9: continue in the new language. Set before the state speaks, so its reply comes in
    it; disclosures and the notice come from the approved translations for the new locale. The
    consent notice pin does not move (I7): only a consent captured on another notice moves it, so S0
    switches its own notice (NOTICE_LANGUAGE)."""
    if session.fsm_state is FsmState.S0 or session.fsm_state in TERMINAL:
        return
    target = handler_tools.language_request(turn, session)
    if target is not None and target != session.locale:
        session.locale = target
        turn.language_switched = True
        logger.info("language switched to %s in %s", target, session.fsm_state)


def _reminder(turn: Turn, session: SessionState) -> bool:
    """TDD §3.9: Aadhaar, card or account numbers (masked at ingress, never stored) or a medical
    report shared after S0: the reply asks the customer not to share them."""
    pipeline = turn.pipeline
    if pipeline is None or pipeline.blocked:
        return False
    if session.fsm_state is FsmState.S0 or session.fsm_state in TERMINAL:
        return False
    lexicon = cast(PromptBundle, turn.bundle).side_query_lexicon
    return pipeline.reminder or mentions(lexicon.medical_report, pipeline.stored_raw)


async def _origin(state: GraphState, runtime: Runtime[Turn]) -> None:
    """The state's own node, guarded as in the graph: it applies what the turn answered and puts
    its pending prompt again."""
    origin = _session(_turn(runtime)).fsm_state
    await states.wrapped(origin, states.NODES[origin])(state, runtime=runtime)


async def side_query(state: GraphState, runtime: Runtime[Turn]) -> None:
    """TDD §2.6, in its order: apply the slot (the origin state's node), answer from a frozen view
    of the session, then resume (the frame popped, the bridge, the state's prompt)."""
    turn = _turn(runtime)
    if (turn.action or {}).get("type") == "FACT":
        lead = side_query_subgraph.fact_action(turn)  # the normal confirmation path
        await _origin(state, runtime)
        turn.parts = [*lead, *turn.parts]
        if lead:
            turn.phrase = None  # a template leads: no generated sentence before the question
        turn.side_outcome = "fact"
        return
    await _origin(state, runtime)
    await side_query_subgraph.respond(turn, turn.side_question or "")


async def objection_node(state: GraphState, runtime: Runtime[Turn]) -> None:
    """CC5 (Step 22): the origin state's node first, then the answer to the objection ahead of its
    prompt; a deferral or the END action skips it (the pause handler or the exit line speaks)."""
    turn = _turn(runtime)
    kind = cast(objection.Kind, turn.objection)
    if kind not in ("deferral", "end"):
        await _origin(state, runtime)
    await objection.respond(turn, kind)


async def timer(state: GraphState, runtime: Runtime[Turn]) -> None:
    """A timer turn (jobs/timers.py): the inactivity, for CC3 (after consent only, V3)."""
    _turn(runtime).signals["inactivity_timeout"] = True


# --- decide ---------------------------------------------------------------------------------------
async def decide(state: GraphState, runtime: Runtime[Turn]) -> Command[str]:
    turn = _turn(runtime)
    session, row = _session(turn), cast(SessionRow, turn.row)
    pipeline = turn.pipeline
    facts = build_facts(
        session,
        pipeline.analysis if pipeline else None,
        pipeline.block_reason if pipeline else None,
        turn.settings,
        action_type=(turn.action or {}).get("type"),
    )
    if turn.signals:  # validated: a wrong name or value fails the turn, never passes silently
        facts = Facts.model_validate(facts.model_dump() | turn.signals)
    before = session.fsm_state
    result = transition(facts, before, turn.settings)
    if result.row_id in ("CC4", "CC5"):
        # Step 22 (TDD §2.6): a side question or an objection is answered, then the customer
        # resumes where the turn's own answer put them. A move that answer earned (the read-back
        # confirmed, an election, a choice) is not held back by the FAQ or objection row.
        earned = transition(
            facts.model_copy(update={"faq": False, "objection": False}), before, turn.settings
        )
        if earned.to is not before:
            result = earned
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
    if result.to is FsmState.DATA_ERASURE and turn.routed in ("side_query", "objection"):
        # Step 22: an erasure (an under-18 age in a compound turn) speaks alone: no answer.
        turn.render, turn.draft, turn.regenerate, turn.handles = None, None, None, None
    if result.to is not before:  # a state's form and quick replies stay in that state
        turn.form, turn.quick_replies = None, []
    return Command(goto=after_decide(turn, before))


def after_decide(turn: Turn, before: FsmState) -> str:
    """The handler for the state the transition entered, else compose. A withdrawal runs the
    erasure even in a closed state, where the FSM stays (I5 is honoured outside it)."""
    to = cast(Transition, turn.transition).to
    if turn.routed == "withdraw_consent" or (
        to is FsmState.DATA_ERASURE and before is not FsmState.DATA_ERASURE
    ):
        return "data_erasure"
    if to is FsmState.HUMAN_ESCALATION and before is not FsmState.HUMAN_ESCALATION:
        return "human_escalation"
    if to is FsmState.PAUSE and before is not FsmState.PAUSE:
        return "pause"
    if to in ENTER and to is not before:
        return ENTER[to]
    return "compose"


# --- compose and validate -------------------------------------------------------------------------
def templates(parts: list[tuple[str, str]]) -> Rendered:
    """Template parts as one message: the texts joined by a blank line, hashed as released (I8).
    A bare id is a bundle template (template:<id>); a namespaced one (registry:<id>,
    notice:<version>) is approved text from its own store, released verbatim."""
    text = "\n\n".join(t for _, t in parts)
    named = [(i if ":" in i else f"template:{i}", t) for i, t in parts]
    return Rendered(text, text_sha256(text), named, {}, [], {}, {})


async def compose(state: GraphState, runtime: Runtime[Turn]) -> None:
    """Template-only until the state steps add cited generation (they set draft, regenerate and
    render, and append MODEL_CALL). A handler's parts come first. An identity question puts the
    AI re-disclosure (I6), and a safety signal the crisis script, before whatever else the turn
    says."""
    turn = _turn(runtime)
    session = _session(turn)
    await _status(turn, "composing")
    overlong = turn.pipeline is not None and turn.pipeline.overlong
    scripts = cast(PromptBundle, turn.bundle).templates[session.locale].scripts
    if turn.render is not None and not (overlong or turn.degraded or turn.identity or turn.safety):
        # Step 21: the state composed its own reply (S3; Step 22, a side answer); the rails check
        # it next. The never-store reminder leads it.
        if turn.reminder:
            own = turn.render
            turn.render = lambda n: s3.with_lead([("pii_reminder", scripts.pii_reminder)], own(n))
        return
    turn.handles, turn.numbers, turn.disclosure_sets, turn.recommendation = None, None, {}, None
    turn.verify_all = False
    chosen: list[tuple[str, str]]
    if turn.parts:
        chosen = list(turn.parts)
    elif turn.pipeline is not None and turn.pipeline.overlong:
        chosen = [("ask_to_shorten", scripts.ask_to_shorten)]
    elif turn.degraded:
        chosen = [("release_blocked", scripts.release_blocked)]
    elif turn.routed == "safety" or turn.identity:
        chosen = []
    else:
        chosen = [("advisor_offer", scripts.advisor_offer)]
    if turn.reminder and not (turn.identity or turn.safety):
        chosen = [("pii_reminder", scripts.pii_reminder), *chosen]
    if turn.identity:
        chosen = [await identity.part(turn), *chosen]
    if turn.safety:
        chosen = [("safety", scripts.safety), *chosen]
    turn.draft = None
    turn.render = lambda _narrative: templates(chosen)
    phrase = turn.phrase
    if (
        phrase is not None
        and session.fsm_state in (FsmState.S1, FsmState.S2)
        and turn.parts
        and not (turn.identity or turn.safety or turn.degraded)
    ):
        turn.products = turn.products or await product_names(turn)
        turn.draft = await converse(turn, phrase)
        turn.regenerate = lambda errors: converse(turn, phrase, errors)
        turn.render = lambda narrative: templates(_lead_with(chosen, narrative))


def _lead_with(parts: list[tuple[str, str]], narrative: str | None) -> list[tuple[str, str]]:
    """The generated sentence just before the question (the last part); none, the template alone."""
    if narrative is None:
        return parts
    return [*parts[:-1], ("generated:narrative", narrative), parts[-1]]


async def converse(
    turn: Turn, phrase: tuple[L1Name, str, tuple[str, ...]], errors: list[str] | None = None
) -> str | None:
    """One friendly sentence from gen-converse with the state's L1 and NEXT_SLOT (Step 19): a
    REDACTED envelope (no number, no identifier), its MODEL_CALL appended. None on any gateway or
    envelope failure: the question template then goes out alone."""
    session = _session(turn)
    l1, next_slot, answered = phrase
    pipeline = turn.pipeline
    try:
        envelope = build_envelope(
            cast(PromptBundle, turn.bundle),
            l1=l1,
            locale=session.locale,
            user_text=pipeline.redacted if pipeline else "",
            facts=SessionFacts(language=LANGUAGE[session.locale], answered_slots=list(answered)),
            next_slot=next_slot,
            corrections=errors or (),
        )
        result = await turn.gateway.call(
            envelope.route,
            data_class=envelope.data_class,
            messages=envelope.messages,
            session_id=turn.session_id,
            turn_id=cast(UUID, turn.out_id),
            fsm_state=session.fsm_state.value,
            attestation=envelope.attestation,
        )
    except (EnvelopeError, GatewayUnavailable) as exc:
        logger.warning("%s phrasing unavailable: %s; the template alone", l1, exc.reason)
        return None
    header, payload = model_call_event(envelope, result)
    append(turn, EventType.MODEL_CALL, header, payload)
    return result.content


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
        route=(
            Route.GEN_RECOMMEND
            if session.fsm_state is FsmState.S3 or turn.verify_all
            else Route.GEN_CONVERSE
        ),
        handles=turn.handles or issue([], []),
        customer_text=turn.pipeline.stored_raw if turn.pipeline else "",
        products=turn.products,
        customer_uins=frozenset(session.focus_uins),
        approved_text=tuple(turn.shown),
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
        numbers=turn.numbers,
        disclosure_sets=turn.disclosure_sets,
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
            # Step 18: the consent form while S0's consent prompt is open, and the quick replies.
            # Not with a blocked release: the reply then is the release-blocked template alone.
            "form": turn.form if rendered else None,
            "quick_replies": turn.quick_replies if rendered else [],
        },
        "documents": [
            {"uin": uin, "documents": docs} for uin, docs in rendered.documents_shown.items()
        ]
        if rendered
        else [],
    }


def _input(turn: Turn) -> dict[str, Any]:
    """An in-turn without text: the structured action, or the timer."""
    return {"timer": TIMER} if turn.timer else cast(dict[str, Any], turn.action)


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
        text=pipeline.stored_raw if pipeline else canonical_json(_input(turn)).decode(),
        redacted=pipeline.redacted
        if pipeline
        else f"[timer:{TIMER}]"
        if turn.timer
        else f"[action:{(turn.action or {}).get('type')}]",
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
    # Step 21: the recommendation as released (I2's hash, the exact text's hash); a blocked
    # release shows no options, so none is recorded and S3 presents again next turn.
    if turn.recommendation is not None and released.rendered is not None:
        shown = released.rendered
        rendered_rec = turn.recommendation.model_copy(
            update={
                "rendered_sha256": shown.rendered_sha256,
                "evidence_map": shown.citations,
                # what each option's render showed: an acknowledgment must match it (V7)
                "shown": {
                    uin: Shown(
                        registry_version=turn.disclosure_sets[uin].registry_version,
                        set_sha256=set_sha256,
                        documents=shown.documents_shown.get(uin, {}),
                    )
                    for uin, set_sha256 in shown.disclosure_hashes.items()
                },
            }
        )
        rec_id = store.insert_recommendation(conn, session_id=turn.session_id, payload=rendered_rec)
        session.recommendation = rendered_rec.model_copy(update={"rec_id": rec_id})
    store.update_session(
        conn,
        turn.session_id,
        fsm_state=session.fsm_state.value,
        frame_stack=[f.model_dump(mode="json") for f in session.stack],
        counters=session.counters,
        pins=session.pins.model_dump(mode="json"),
        locale=session.locale,
        status="erased" if turn.erasure else session_status(session.fsm_state),
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
            language=language,
            faq=turn.side_outcome if turn.routed == "side_query" else None,
            objection=turn.objection if turn.routed == "objection" else None,
            objection_response=turn.objection_response if turn.routed == "objection" else None,
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
    graph.add_node(
        "route",
        route,
        destinations=(*ROUTED, "side_query", "objection", "timer", *state_names),
    )
    for name, step in (("side_query", side_query), ("objection", objection_node), ("timer", timer)):
        graph.add_node(name, step)
        graph.add_edge(name, "decide")
    for fsm_state, node in states.NODES.items():
        graph.add_node(fsm_state.value, states.wrapped(fsm_state, node))
        graph.add_edge(fsm_state.value, "decide")
    for name, handler in ROUTED.items():
        graph.add_node(name, handler)
        graph.add_edge(name, "decide")
    graph.add_node("decide", decide, destinations=(*ENTERED, "compose"))
    for name, handler in ENTERED.items():
        graph.add_node(name, handler)
        graph.add_edge(name, "compose")
    graph.add_node("compose", compose)
    graph.add_node("validate", validate)
    graph.add_node("commit", commit)
    graph.add_node("release", release_node)
    graph.add_edge(START, "load")
    graph.add_edge("input", "route")
    graph.add_edge("compose", "validate")
    graph.add_edge("validate", "commit")
    graph.add_edge("commit", "release")
    graph.add_edge("release", END)
    return graph.compile(checkpointer=checkpointer)
