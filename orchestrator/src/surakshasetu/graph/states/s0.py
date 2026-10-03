"""The S0 node: State-0, greeting, AI disclosure and consent (TDD §3.5).
A stub until Step 18 replaces it."""

from typing import Any

from langgraph.runtime import Runtime

from surakshasetu.graph.state import GraphState


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """No S0 logic yet: decide calls fsm.transition() next."""
