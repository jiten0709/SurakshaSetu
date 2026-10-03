"""The S3 node: State-3, recommendation, comparison and disclosure (TDD §3.8).
A stub until Step 21 replaces it."""

from typing import Any

from langgraph.runtime import Runtime

from surakshasetu.graph.state import GraphState


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """No S3 logic yet: decide calls fsm.transition() next."""
