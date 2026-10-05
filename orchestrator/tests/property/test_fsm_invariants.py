"""Hypothesis properties of the pure transition function (TDD §3.1-3.2, Step 15).

Every input is checked against every property; each property restates its rule from the Facts
directly, so a wrong row table fails here even when the function's own invariants dict agrees with
it. Two sources of input:
- Hypothesis (arbitrary Facts, and every row's case with up to three fields redrawn), with a budget
  large enough to reach every row: test_the_draws_reach_every_row keeps that true;
- an exhaustive sweep of every row's case with any one field set to any value of its domain, which
  random redraws reach too rarely (a mutation dropping the V7 acks check slipped past them).
I3, I4, I6, I7 and I8 are output and runtime properties; the golden harness asserts them (Step 17).
"""

from collections import Counter

from fsm_support import CASES, DOMAINS, TH, Th, scenarios, thresholds
from hypothesis import given, settings

from surakshasetu.fsm.facts import Facts
from surakshasetu.fsm.rows import CROSS_CUTTING, Thresholds, evaluation_rows
from surakshasetu.fsm.states import NEEDS_P1, TERMINAL, FsmState
from surakshasetu.fsm.transition import Transition, transition

S = FsmState
BUDGET = 3000  # about 3 s; every row id is reached well within it


def exactly_one_row_decides(f: Facts, state: FsmState, th: Thresholds, t: Transition) -> None:
    rows = evaluation_rows(state)
    keys = [(0 if row in CROSS_CUTTING else 1, row.order) for row in rows]
    matched = [row for row in rows if row.condition(f, th)]

    assert keys == sorted(set(keys))  # strictly increasing: no two rows share an order
    assert matched  # total: the STAY row always matches
    if matched[0].to != "RESUME":  # a resume reports the resumed state's row (test_fsm_rows)
        assert t.row_id == matched[0].id


def i1_i2_i5_hold(f: Facts, state: FsmState, t: Transition) -> None:
    if state in TERMINAL:
        assert set(t.invariants) == {"I5"}
        return
    if t.to in NEEDS_P1:  # I1
        assert f.consent == "valid"
    if t.to is S.S3:  # I2
        assert f.suitability is not None
        assert f.suitability.inputs_sha256 == f.needs_slots_sha256
    if f.withdraw:  # I5
        assert t.to is S.DATA_ERASURE
    assert t.invariants == {"I1": True, "I2": True, "I5": True}


def quote_only_never_hands_off(f: Facts, state: FsmState, t: Transition) -> None:
    if state is S.QUOTE_ONLY or (state is S.PAUSE and f.paused_from is S.QUOTE_ONLY):
        assert t.to is not S.HANDOFF


def the_hand_off_needs_plan_quote_and_acks(f: Facts, state: FsmState, t: Transition) -> None:
    if state is not S.HANDOFF and t.to is S.HANDOFF:  # V7
        assert f.selected is not None
        assert f.selected.quote_valid
        assert f.acks_valid_for_selected


def mandatory_triggers_escalate(f: Facts, state: FsmState, t: Transition) -> None:
    # Unless an erasure comes first: CC1 (withdraw) and CC1b (minor) precede CC2.
    if state not in TERMINAL and f.mandatory_trigger is not None and not (f.withdraw or f.minor):
        assert (t.to, t.reason_code) == (S.HUMAN_ESCALATION, f.mandatory_trigger.value)


def terminal_states_stay(state: FsmState, t: Transition) -> None:
    if state in TERMINAL:
        assert (t.to, t.row_id) == (state, f"{state.value}.STAY")


def pre_consent_inactivity_never_pauses(f: Facts, state: FsmState, t: Transition) -> None:
    # V3. Staying in PAUSE is not entering it, so PAUSE itself is left out. A dependency down
    # (Step 20) pauses only after consent too.
    pre_consent = f.consent in ("none", "refused")
    paused_by = f.inactivity_timeout or f.dependency_down
    if state not in TERMINAL | {S.PAUSE} and pre_consent and paused_by:
        assert t.to is not S.PAUSE


def check(f: Facts, state: FsmState, th: Thresholds) -> Transition:
    t = transition(f, state, th)
    exactly_one_row_decides(f, state, th, t)
    i1_i2_i5_hold(f, state, t)
    quote_only_never_hands_off(f, state, t)
    the_hand_off_needs_plan_quote_and_acks(f, state, t)
    mandatory_triggers_escalate(f, state, t)
    terminal_states_stay(state, t)
    pre_consent_inactivity_never_pauses(f, state, t)
    return t


@settings(max_examples=BUDGET, deadline=None)
@given(scenarios, thresholds)
def test_every_property_holds_for_every_input(
    scenario: tuple[FsmState, Facts], th: Thresholds
) -> None:
    state, f = scenario
    check(f, state, th)


def test_the_draws_reach_every_row() -> None:
    """Non-vacuity, derandomized so it cannot flake: the same budget reaches every row id, and
    the premises of the I2 and hand-off properties."""
    rows: Counter[str] = Counter()
    entered: Counter[FsmState] = Counter()

    @settings(max_examples=BUDGET, deadline=None, derandomize=True, database=None)
    @given(scenarios, thresholds)
    def draw(scenario: tuple[FsmState, Facts], th: Thresholds) -> None:
        state, f = scenario
        t = check(f, state, th)
        rows[t.row_id] += 1
        if t.to is not state:
            entered[t.to] += 1

    draw()

    assert set(CASES) <= set(rows), sorted(set(CASES) - set(rows))
    assert entered[S.S3] and entered[S.HANDOFF], entered


def test_the_domains_cover_every_facts_field() -> None:
    assert set(DOMAINS) == set(Facts.model_fields)


def test_every_one_field_change_of_every_case_keeps_every_property() -> None:
    checked = 0
    for row_id, case in CASES.items():
        for field, values in DOMAINS.items():
            for value in values:
                f = case.positive.model_copy(update={field: value})
                for th in (TH, Th(0.0, 1, 1), Th(1.0, 3, 3)):
                    try:
                        check(f, case.state, th)
                    except AssertionError as exc:
                        raise AssertionError(f"{row_id}: {field}={value!r}, {th}") from exc
                    checked += 1

    assert checked > 10_000
