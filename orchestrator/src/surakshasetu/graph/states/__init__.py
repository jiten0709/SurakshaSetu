"""Per-state graph nodes. S0 is State-0 (Step 18), S1 and QUOTE_ONLY are Step 19's, S2 is Step
20's; S3 stays a stub until Step 21 replaces it (it calls s1.correction, then s2.correction: V4).
Of the support states, HUMAN_ESCALATION and PAUSE run their Step 17 handlers (the P2 answer, and
resume); the closed ones stay stubs.

A state node works on the turn's scratch (runtime.context: graph.nodes.Turn) and never chooses the
next state: decide calls fsm.transition() after it. `guarded` is the dependency-down plumbing of
TDD §3.9: a domain call that times out, is unreachable or answers 5xx marks the turn degraded, and
compose answers with the state's fallback (S1 template question and retry, S2 save and resume, S3
the deterministic card or save and resume; the state steps write the wording). PAUSE is not
guarded: resume fails closed rather than resume on stale consent (graph/handlers/pause.py).
"""

import functools
import logging
from collections.abc import Awaitable
from typing import Any, Protocol

from langgraph.runtime import Runtime

from surakshasetu.domain.client import DomainError
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph.handlers import human_escalation, pause
from surakshasetu.graph.state import GraphState
from surakshasetu.graph.states import quote_only, s0, s1, s2, s3

logger = logging.getLogger(__name__)


class Node(Protocol):
    def __call__(self, state: GraphState, *, runtime: Runtime[Any]) -> Awaitable[None]: ...


async def stub(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """No per-state logic yet."""


def guarded(name: str, node: Node) -> Node:
    @functools.wraps(node)
    async def run(state: GraphState, *, runtime: Runtime[Any]) -> None:
        try:
            await node(state, runtime=runtime)
        except DomainError as exc:
            if exc.status is not None and exc.status < 500:
                raise  # a contract problem, not an outage
            logger.warning("domain tier down in %s: %s; degraded reply", name, exc.code)
            runtime.context.degraded = True

    return run


def wrapped(state: FsmState, node: Node) -> Node:
    return node if state is FsmState.PAUSE else guarded(state.value, node)


# The slot names nlu-extract is told about in each state (PendingSlotSpec.known_slots): S1's,
# Quote-Only's and S2's, and the eligibility and needs facts a later state may correct (V4).
KNOWN_SLOTS: dict[FsmState, tuple[str, ...]] = {
    FsmState.S1: tuple(sorted(s1.ELIGIBILITY)),
    FsmState.QUOTE_ONLY: (*quote_only.QUOTE_SLOTS, *quote_only.STATED),
    FsmState.S2: (*s2.NEEDS, *sorted(s1.ELIGIBILITY)),
    FsmState.S3: (*sorted(s1.ELIGIBILITY), *s2.NEEDS),
}

NODES: dict[FsmState, Node] = {state: stub for state in FsmState} | {
    FsmState.S0: s0.node,
    FsmState.S1: s1.node,
    FsmState.QUOTE_ONLY: quote_only.node,
    FsmState.S2: s2.node,
    FsmState.S3: s3.node,
    FsmState.HUMAN_ESCALATION: human_escalation.node,
    FsmState.PAUSE: pause.resume,
}
