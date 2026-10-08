"""Pre-rail faithfulness and citation precision (TDD §5.2, Step 23): the model's drafts before any
output rail, which the audit does not keep (MODEL_CALL holds the envelope). So each answerable
retrieval golden question is asked as a side question in S3: the evidence the gate kept (from
retrieval_metrics' run, so the rerank cache is shared), the side-query envelope, gen-recommend.

RAGAS definitions (decided 2026-10-06, no library): the claims are the draft's factual sentences,
split and recognised as the output rails do; a claim is supported when it cites evidence the
verify-claims route says entails it (an uncited claim is unsupported). Citation precision is one
verify-claims call per (sentence, cited chunk) pair. Against the stub the drafts state no facts, so
both read n/a: the run is a smoke test of the live path.
"""

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

from surakshasetu.compose.bundle import PromptBundle
from surakshasetu.compose.envelope import EnvelopeError, SessionFacts, build
from surakshasetu.eval.metrics import ratio
from surakshasetu.eval.retrieval_metrics import Outcome
from surakshasetu.gateway import Gateway, GatewayUnavailable, Route
from surakshasetu.rails import redact
from surakshasetu.rails.normalise import normalise
from surakshasetu.rails.output import (
    LexiconPack,
    OutputContext,
    entailed,
    factual,
    sentences,
    sources,
)
from surakshasetu.retrieval.gate import Thresholds
from surakshasetu.retrieval.service import RetrievalService
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger(__name__)


@dataclass
class Draft:
    question_id: str
    language: str
    abstained: bool = False
    error: str | None = None
    served_model: str | None = None
    claims: list[bool] = field(default_factory=list)  # per factual sentence: cited and entailed
    pairs: list[bool] = field(default_factory=list)  # per (sentence, cited chunk): entailed
    cited: list[bool] = field(default_factory=list)  # per factual sentence: cites a handle


async def run_qa(
    gateway: Gateway,
    service: RetrievalService,
    outcomes: Sequence[Outcome],
    thresholds: Thresholds | None,
    bundle: PromptBundle,
    pack: LexiconPack,
) -> list[Draft]:
    drafts = []
    for outcome in outcomes:
        question = outcome.question
        if question.collection is None:
            continue
        draft = Draft(question.id, question.language)
        drafts.append(draft)
        result = service.decide(outcome.selection, thresholds)
        if result.abstained:
            draft.abstained = True
            continue
        locale = "hi-IN" if question.language == "hi" else "en-IN"
        try:
            envelope = build(
                bundle,
                l1="side-query",
                locale=locale,
                user_text=redact.redact(normalise(question.text).text).redacted,
                facts=SessionFacts(language=question.language),
                retrieval=result,
            )
            ctx = OutputContext(
                session_id=uuid7(),
                turn_id=uuid7(),
                subject_ref=uuid7(),
                fsm_state="S3",
                pins={},
                key_ref="eval",
                locale=locale,
                route=Route.GEN_RECOMMEND,
                handles=envelope.handles,
                customer_text="",
            )
            reply = await gateway.call(
                envelope.route,
                data_class=envelope.data_class,
                messages=envelope.messages,
                session_id=ctx.session_id,
                turn_id=ctx.turn_id,
                fsm_state="S3",
                attestation=envelope.attestation,
            )
            draft.served_model = reply.served_model
            await _judge(draft, reply.content, ctx, gateway, pack)
        except (EnvelopeError, GatewayUnavailable) as exc:
            draft.error = exc.reason
            logger.warning("qa %s: %s", question.id, exc.reason)
    logger.info(
        "qa: %d drafts, %d abstained, %d errors",
        len(drafts),
        sum(d.abstained for d in drafts),
        sum(d.error is not None for d in drafts),
    )
    return drafts


async def _judge(
    draft: Draft, text: str, ctx: OutputContext, gateway: Gateway, pack: LexiconPack
) -> None:
    issued = set(ctx.handles.evidence) | set(ctx.handles.engine)
    for sentence in sentences(normalise(text).text):
        handles = [h for h in dict.fromkeys(sentence.handles) if h in issued]
        for handle in handles:
            premise = "\n".join(sources([handle], ctx.handles))
            draft.pairs.append(await entailed(gateway, sentence.text, premise, ctx))
        if factual(sentence.text, pack):
            draft.cited.append(bool(handles))
            premise = "\n".join(sources(handles, ctx.handles))
            draft.claims.append(
                bool(handles) and await entailed(gateway, sentence.text, premise, ctx)
            )


def scores(drafts: Sequence[Draft], attribute: str) -> dict[str, float | None]:
    """A ratio over all drafts ("all") and per language: claims (faithfulness), pairs (citation
    precision) or cited (coverage of the drafts)."""
    per: dict[str, list[bool]] = defaultdict(list)
    for d in drafts:
        flags = getattr(d, attribute)
        per["all"] += flags
        per[d.language] += flags
    return {k: ratio(sum(v), len(v)) for k, v in per.items()}
