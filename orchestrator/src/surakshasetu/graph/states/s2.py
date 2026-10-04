"""The S2 node: State-2, needs discovery and suitability (TDD §3.7).
A stub until Step 20 replaces it. It already runs the V4 hook (Step 19); Step 20 keeps it
first."""

from typing import Any

from langgraph.runtime import Runtime

from surakshasetu.graph.state import GraphState
from surakshasetu.graph.states import s1


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """No S2 logic yet: a corrected eligibility fact re-runs S1's rows (G2, V4); decide calls
    fsm.transition() next."""
    await s1.correction(runtime.context)
