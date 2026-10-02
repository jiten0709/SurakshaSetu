"""`python -m surakshasetu.fsm.mermaid` prints the state diagram the rows encode, in the shape of
TDD §3.1's first diagram: the state rows only, without STAY rows. The cross-cutting rows apply in
every state, so like the TDD the diagram leaves them off; it lists them as comments, in order.
"""

import sys

from surakshasetu.fsm.rows import CROSS_CUTTING, STATE_ROWS
from surakshasetu.fsm.states import FsmState

S = FsmState

ALIAS = {
    S.S0: "S0",
    S.S1: "S1",
    S.QUOTE_ONLY: "QO",
    S.S2: "S2",
    S.S3: "S3",
    S.HUMAN_ESCALATION: "HE",
    S.PAUSE: "Pause",
    S.EXIT: "Exit",
    S.EXIT_ADVISORY: "XA",
    S.DATA_ERASURE: "DE",
    S.HANDOFF: "HO",
}
LABEL = {
    S.S0: "S0 Greeting, consent",
    S.S1: "S1 Eligibility",
    S.QUOTE_ONLY: "Quote-Only",
    S.S2: "S2 Needs, suitability",
    S.S3: "S3 Recommendation",
    S.HANDOFF: "Hand-off (S4 intake)",
    S.HUMAN_ESCALATION: "Human Escalation",
    S.EXIT_ADVISORY: "Exit (Advisory)",
    S.DATA_ERASURE: "Data Erasure",
}
JOURNEY = (S.S0, S.S1, S.QUOTE_ONLY, S.S2, S.S3)


def _target(to: object) -> str:
    return ALIAS[to] if isinstance(to, FsmState) else str(to)


def render() -> str:
    lines = ["stateDiagram-v2"]
    lines += [f'    state "{label}" as {ALIAS[state]}' for state, label in LABEL.items()]
    lines.append("    [*] --> S0")
    for state in JOURNEY:
        lines += [
            f"    {ALIAS[state]} --> {ALIAS[row.to]}: {row.trigger}"
            for row in STATE_ROWS[state]
            if isinstance(row.to, FsmState)
        ]
    lines += [
        f"    Pause --> {ALIAS[state]}: resume"
        for state in JOURNEY
        if any(row.to is S.PAUSE for row in STATE_ROWS[state])
    ]
    lines.append("    note right of QO : No path to the hand-off (C13)")
    lines.append("    %% Cross-cutting rows, tried first in their states, in this order:")
    lines += [f"    %% {row.id} -> {_target(row.to)}: {row.trigger}" for row in CROSS_CUTTING]
    return "\n".join(lines) + "\n"


def main() -> None:
    sys.stdout.write(render())


if __name__ == "__main__":
    main()
