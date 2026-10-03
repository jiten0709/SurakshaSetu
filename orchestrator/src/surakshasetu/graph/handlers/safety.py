"""Safety (a mandatory trigger, TDD §3.9). A self-harm signal routes the turn here, past the state
node, so sales stop. build_facts raises HE_SAFETY, so CC2 escalates. compose prepends the crisis
script and helpline template to whatever the handlers say in the same turn, whether that is the
escalation's message or, when a withdrawal wins the router, the erasure confirmation."""

from typing import Any

from langgraph.runtime import Runtime

from surakshasetu.analysis.models import Intent
from surakshasetu.analysis.pipeline import PipelineResult
from surakshasetu.graph.state import GraphState


def signal(pipeline: PipelineResult | None) -> bool:
    """The SAFETY intent, or the safety rail's block (which fails closed on a self-harm lexicon hit
    even when guard-input is down)."""
    if pipeline is None:
        return False
    intents = pipeline.analysis.intents if pipeline.analysis else []
    return Intent.SAFETY in intents or pipeline.block_reason == "safety"


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """The turn router's target: no state processing in this turn."""
