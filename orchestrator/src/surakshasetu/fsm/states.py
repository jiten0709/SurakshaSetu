"""The conversation's states (TDD §3.1, the guide's FSM state table). S4-S8 and Dormant are phase 2:
the S3 -> S4 row targets HANDOFF, the phase-1 hand-off adapter (TDD §7.1)."""

from enum import StrEnum


class FsmState(StrEnum):
    S0 = "S0"
    S1 = "S1"
    QUOTE_ONLY = "QUOTE_ONLY"
    S2 = "S2"
    S3 = "S3"
    HUMAN_ESCALATION = "HUMAN_ESCALATION"
    PAUSE = "PAUSE"
    EXIT = "EXIT"
    EXIT_ADVISORY = "EXIT_ADVISORY"
    DATA_ERASURE = "DATA_ERASURE"
    HANDOFF = "HANDOFF"


S = FsmState

# The session is closed: no row leaves these.
TERMINAL = frozenset({S.HUMAN_ESCALATION, S.EXIT, S.EXIT_ADVISORY, S.DATA_ERASURE, S.HANDOFF})
# PAUSE resumes to the paused-from state, one of these (S0 when it is unknown).
RESUMABLE = frozenset({S.S0, S.S1, S.QUOTE_ONLY, S.S2, S.S3})
# I1: reaching any of these needs a valid P1 consent record.
NEEDS_P1 = frozenset({S.S1, S.QUOTE_ONLY, S.S2, S.S3, S.HANDOFF})
