"""transition(facts, state, thresholds) -> Transition (TDD §3.2): pure and total.

No I/O, no logging and no clock: the caller (Step 16's decide node) logs the row id and the state
change, and records them in the STATE_TRANSITION audit header. LangGraph edges only call this.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

from surakshasetu.fsm.facts import Facts
from surakshasetu.fsm.rows import Thresholds, evaluation_rows
from surakshasetu.fsm.states import NEEDS_P1, RESUMABLE, TERMINAL, FsmState


class Transition(BaseModel):
    model_config = ConfigDict(frozen=True)

    to: FsmState
    row_id: str
    reason_code: str
    subgraph: Literal["side_query", "objection"] | None = None
    # Only what the transition can check: I1, I2 and I5 (I5 alone from a closed session). The
    # golden harness asserts I3, I4, I6, I7 and I8 (Step 17); they are left out, never claimed.
    invariants: dict[str, bool]


def transition(facts: Facts, state: FsmState, thresholds: Thresholds) -> Transition:
    row = next(r for r in evaluation_rows(state) if r.condition(facts, thresholds))
    if row.to == "RESUME":
        target = facts.paused_from if facts.paused_from in RESUMABLE else FsmState.S0
        resumed = transition(facts, target, thresholds)
        if resumed.row_id != evaluation_rows(target)[-1].id:
            return resumed  # the resumed state's rows moved on (or its guards sent it back)
        return resumed.model_copy(update={"row_id": row.id, "reason_code": row.reason(facts)})
    to = row.to if isinstance(row.to, FsmState) else state
    return Transition(
        to=to,
        row_id=row.id,
        reason_code=row.reason(facts),
        subgraph=row.subgraph,
        invariants=_invariants(facts, state, to),
    )


def _invariants(facts: Facts, state: FsmState, to: FsmState) -> dict[str, bool]:
    honoured = not facts.withdraw or to is FsmState.DATA_ERASURE
    if state in TERMINAL:  # a closed session: a withdrawal here needs Step 17's erasure path
        return {"I5": honoured}
    return {
        "I1": to not in NEEDS_P1 or facts.valid_p1,
        "I2": to is not FsmState.S3 or facts.suitability_current,
        "I5": honoured,
    }
