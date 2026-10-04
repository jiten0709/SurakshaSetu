"""I6 (TDD §3.2, §3.5): "Am I talking to a person?" always gets the truthful, templated answer, in
any state. The question is detected deterministically, from the bundle's closed phrase list
(lexicons/identity_question.yaml) on the normalised turn, so a model outage cannot hide it. compose
puts the `ai_redisclosure` template, filled with the registry's DISC-GLOBAL-AI-06 body verbatim,
before anything else the turn says (after the crisis script, when that is signalled).

The S0 greeting shows the same registry body (graph/states/s0.py); both fetch it through
`disclosure`, once per turn.
"""

import logging
from typing import TYPE_CHECKING

from surakshasetu.analysis.pipeline import PipelineResult
from surakshasetu.compose.bundle import PromptBundle, mentions
from surakshasetu.domain.client import DomainError
from surakshasetu.graph.handlers import bundle, scripts, session

if TYPE_CHECKING:
    from surakshasetu.graph.nodes import Turn

logger = logging.getLogger(__name__)

AI_DISCLOSURE = "DISC-GLOBAL-AI-06"


def asks(prompts: PromptBundle, pipeline: PipelineResult | None) -> bool:
    """True when the turn contains an identity question as whole words."""
    return pipeline is not None and mentions(prompts.identity_lexicon.phrases, pipeline.stored_raw)


async def disclosure(turn: "Turn") -> str | None:
    """DISC-GLOBAL-AI-06 in the session's language, verbatim, fetched once per turn. None when the
    registry cannot give it; the caller decides what that means."""
    if turn.ai_disclosure is None:
        try:
            found = await turn.domain.get_disclosure(AI_DISCLOSURE, session(turn).locale)
        except DomainError as exc:
            logger.warning("AI disclosure unavailable from the registry: %s", exc.code)
            return None
        turn.ai_disclosure = found.body
        turn.shown.append(found.body)
    return turn.ai_disclosure


async def part(turn: "Turn") -> tuple[str, str]:
    """The re-disclosure. Without the registry body it still says, truthfully, that this is an AI
    and not a human advisor: the template's own first line."""
    body = await disclosure(turn)
    if body is None:
        logger.warning("AI re-disclosure released without the registry body")
    text = scripts(turn).ai_redisclosure.format(
        insurer=bundle(turn).manifest.insurer, disclosure=body or ""
    )
    return "ai_redisclosure", text.rstrip()
