"""The S2 node: State-2, needs discovery and suitability (TDD §3.7).
A stub until Step 20 replaces it."""

from typing import Any

from langgraph.runtime import Runtime

from surakshasetu.graph.state import GraphState


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """No S2 logic yet: decide calls fsm.transition() next."""
