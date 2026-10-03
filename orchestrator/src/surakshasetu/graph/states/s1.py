"""The S1 node: State-1, rapport and eligibility screening (TDD §3.6).
A stub until Step 19 replaces it."""

from typing import Any

from langgraph.runtime import Runtime

from surakshasetu.graph.state import GraphState


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """No S1 logic yet: decide calls fsm.transition() next."""
