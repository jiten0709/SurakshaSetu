"""TDD §1.4 stages 2-3: analyse_turn runs the input rails and turn analysis concurrently, applies
the rail verdicts (a block discards the analysis except for a withdrawal, I5), and audits every
rail plus the turn itself. The caller supplies the transaction; nothing here commits.

Step 10 does not write conv.turn/conv.slot_value rows: surakshasetu/store/ (Step 16) owns
persisting conversation rows. The one exception is conv.session.counters, which the task spec
names directly.
"""

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from surakshasetu.analysis.models import Intent, TurnAnalysis
from surakshasetu.analysis.nlu import PendingSlotSpec
from surakshasetu.analysis.nlu import extract as nlu_extract
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.chain import Conn
from surakshasetu.audit.events import EventType, GuardVerdictHeader, TurnInputHeader
from surakshasetu.config import Settings
from surakshasetu.crypto.keys import KeyService
from surakshasetu.gateway import Gateway, GatewayUnavailable
from surakshasetu.rails import injection, redact, safety
from surakshasetu.rails.normalise import NormaliseResult, normalise

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TurnContext:
    session_id: UUID
    turn_id: UUID
    turn_seq: int
    turn_key: UUID
    fsm_state: str
    pins: Mapping[str, Any]
    channel: Literal["web", "app"]
    key_ref: str
    pending: PendingSlotSpec


@dataclass(frozen=True)
class PipelineResult:
    analysis: TurnAnalysis | None  # None => nlu-extract failed; caller asks a template question
    stored_raw: str
    redacted: str
    language: Literal["en", "hi", "hi-Latn"]
    overlong: bool
    reminder: bool
    blocked: bool
    block_reason: Literal["injection", "safety"] | None


@dataclass(frozen=True)
class Analysed:
    """What `analyse` found, before anything is written: the result and the rail verdicts the
    audit records."""

    result: PipelineResult
    normalised: NormaliseResult
    redaction: redact.RedactResult
    injection: injection.InjectionVerdict
    safety: safety.SafetyVerdict


async def analyse(
    gateway: Gateway,
    raw_text: str,
    *,
    session_id: UUID,
    turn_id: UUID,
    fsm_state: str,
    pending: PendingSlotSpec,
    settings: Settings,
) -> Analysed:
    """The input rails and turn analysis, with no database: what analyse_turn audits, and what the
    offline evaluation (Step 23) measures, so both read a turn the same way."""
    normalised = normalise(raw_text, token_cap=settings.normalise_token_cap)
    redaction = redact.redact(normalised.text)

    async def _rails() -> tuple[injection.InjectionVerdict, safety.SafetyVerdict]:
        guard = await injection.call_guard(
            gateway,
            text=redaction.stored_raw,
            session_id=session_id,
            turn_id=turn_id,
            fsm_state=fsm_state,
        )
        inj = injection.evaluate(
            redaction.stored_raw, guard, threshold=settings.injection_score_threshold
        )
        saf = safety.evaluate(redaction.stored_raw, guard)
        return inj, saf

    async def _nlu() -> TurnAnalysis | None:
        try:
            return await nlu_extract(
                gateway,
                text=redaction.stored_raw,
                pending=pending,
                session_id=session_id,
                turn_id=turn_id,
                fsm_state=fsm_state,
            )
        except GatewayUnavailable as exc:
            logger.warning("nlu-extract unavailable: %s", exc.reason)
            return None

    (inj_verdict, safety_verdict), analysis = await asyncio.gather(_rails(), _nlu())

    blocked = inj_verdict.hit or safety_verdict.hit
    block_reason: Literal["injection", "safety"] | None = (
        "injection" if inj_verdict.hit else ("safety" if safety_verdict.hit else None)
    )
    if blocked and analysis is not None:
        kept = [i for i in analysis.intents if i is Intent.META_WITHDRAW]
        analysis = analysis.model_copy(update={"slots": [], "side_query": None, "intents": kept})

    result = PipelineResult(
        analysis=analysis,
        stored_raw=redaction.stored_raw,
        redacted=redaction.redacted,
        language=normalised.language,
        overlong=normalised.overlong,
        reminder=redaction.reminder,
        blocked=blocked,
        block_reason=block_reason,
    )
    return Analysed(result, normalised, redaction, inj_verdict, safety_verdict)


async def analyse_turn(
    conn: Conn,
    keys: KeyService,
    gateway: Gateway,
    raw_text: str,
    ctx: TurnContext,
    *,
    settings: Settings,
) -> PipelineResult:
    found = await analyse(
        gateway,
        raw_text,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        fsm_state=ctx.fsm_state,
        pending=ctx.pending,
        settings=settings,
    )
    if found.injection.hit:
        conn.execute(
            "UPDATE conv.session SET counters = jsonb_set(counters, '{injection}', "
            "(COALESCE(counters->>'injection', '0')::int + 1)::text::jsonb)"
            " WHERE session_id = %s",
            (ctx.session_id,),
        )

    _emit_audit(conn, keys, ctx, found.normalised, found.redaction, found.injection, found.safety)
    return found.result


def _emit_audit(
    conn: Conn,
    keys: KeyService,
    ctx: TurnContext,
    normalised: NormaliseResult,
    redaction: redact.RedactResult,
    inj_verdict: injection.InjectionVerdict,
    safety_verdict: safety.SafetyVerdict,
) -> None:
    audit_chain.append(
        conn,
        keys,
        session_id=ctx.session_id,
        event_type=EventType.TURN_INPUT,
        fsm_state=ctx.fsm_state,
        pins=ctx.pins,
        header=TurnInputHeader(
            turn_id=ctx.turn_id,
            turn_seq=ctx.turn_seq,
            language=normalised.language,
            channel=ctx.channel,
            turn_key=ctx.turn_key,
        ),
        payload={"stored_raw": redaction.stored_raw, "redacted": redaction.redacted},
        key_ref=ctx.key_ref,
    )
    rail_verdicts: tuple[tuple[str, str, float | None, str], ...] = (
        (
            "normalise",
            "overlong" if normalised.overlong else "none",
            None,
            "ask_to_shorten" if normalised.overlong else "allow",
        ),
        (
            "redact",
            "never_store_hit" if redaction.reminder else "none",
            None,
            "remind" if redaction.reminder else "allow",
        ),
        (
            "injection",
            inj_verdict.rule_id,
            inj_verdict.score,
            "discard_slots" if inj_verdict.hit else "allow",
        ),
        (
            "safety",
            safety_verdict.rule_id,
            None,
            "safety_handler" if safety_verdict.hit else "allow",
        ),
    )
    for rail, rule_id, score, action in rail_verdicts:
        audit_chain.append(
            conn,
            keys,
            session_id=ctx.session_id,
            event_type=EventType.GUARD_VERDICT,
            fsm_state=ctx.fsm_state,
            pins=ctx.pins,
            header=GuardVerdictHeader(rail=rail, rule_id=rule_id, score=score, action=action),
            payload={},
            key_ref=ctx.key_ref,
        )
