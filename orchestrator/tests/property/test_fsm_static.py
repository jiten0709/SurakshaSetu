"""Static checks on the fsm (Step 15): row tables, import isolation, the contract's literals,
Settings as the thresholds, and the Mermaid diagram."""

import ast
import sys
from pathlib import Path
from typing import get_args

import pytest
from fsm_support import TH, consented, in_s3

from surakshasetu.config import Settings
from surakshasetu.domain import models
from surakshasetu.fsm import facts as fsm_facts
from surakshasetu.fsm import mermaid
from surakshasetu.fsm.facts import Facts
from surakshasetu.fsm.rows import CROSS_CUTTING, STATE_ROWS, Thresholds, evaluation_rows
from surakshasetu.fsm.states import RESUMABLE, TERMINAL, FsmState

FSM = Path(fsm_facts.__file__).parent


def test_every_state_has_a_row_table_ending_in_an_unconditional_stay() -> None:
    assert set(STATE_ROWS) == set(FsmState)
    for state, rows in STATE_ROWS.items():
        orders = [row.order for row in rows]
        stay = rows[-1]

        assert len(orders) == len(set(orders)), state
        assert orders == sorted(orders), state
        assert (stay.id, stay.to) == (f"{state.value}.STAY".replace("QUOTE_ONLY", "QO"), "STAY")
        assert all(stay.condition(f, TH) for f in (Facts(), consented(), in_s3()))


def test_cross_cutting_orders_and_all_row_ids_are_unique() -> None:
    orders = [row.order for row in CROSS_CUTTING]
    ids = [row.id for row in CROSS_CUTTING] + [r.id for rows in STATE_ROWS.values() for r in rows]

    assert orders == sorted(set(orders))
    assert len(ids) == len(set(ids))


def test_cross_cutting_rows_follow_tdd_3_9_with_the_guards_before_faq_and_objection() -> None:
    # Step 22: the deferral pause (CC3b) and the exit after a repeated objection (CC5b) come last.
    assert [row.id for row in CROSS_CUTTING] == [
        "CC1", "CC1b", "CC2", "CC3", "G1", "G2", "G3", "G4", "CC4", "CC5", "CC3b", "CC5b",
    ]  # fmt: skip


def test_terminal_states_evaluate_only_their_stay_row() -> None:
    for state in TERMINAL:
        assert [row.id for row in evaluation_rows(state)] == [STATE_ROWS[state][-1].id]


def test_pause_runs_only_the_erasure_and_escalation_rows_before_resuming() -> None:
    assert [row.id for row in evaluation_rows(FsmState.PAUSE)] == [
        "CC1", "CC1b", "CC2", "PAUSE.R", "PAUSE.STAY",
    ]  # fmt: skip


def test_targets_are_states_except_stay_and_one_resume() -> None:
    rows = [*CROSS_CUTTING, *(r for rows in STATE_ROWS.values() for r in rows)]
    resumes = [row.id for row in rows if row.to == "RESUME"]

    assert all(isinstance(row.to, FsmState) or row.to in ("STAY", "RESUME") for row in rows)
    assert resumes == ["PAUSE.R"]
    assert FsmState.PAUSE not in RESUMABLE


def test_the_fsm_imports_only_the_stdlib_typing_and_pydantic() -> None:
    allowed = set(sys.stdlib_module_names) | {"pydantic"}
    offenders = []
    for path in sorted(FSM.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else []
            )
            offenders += [
                f"{path.name}: {name}"
                for name in names
                if name.split(".")[0] not in allowed and not name.startswith("surakshasetu.fsm")
            ]

    assert not offenders, offenders


@pytest.mark.parametrize(
    ("mirror", "model", "field"),
    [
        (fsm_facts.EligibilityOutcome, models.EligibilityResult, "outcome"),
        (fsm_facts.SuitabilityOutcome, models.SuitabilityResult, "outcome"),
        (fsm_facts.Affordability, models.SuitabilityResult, "affordability"),
    ],
)
def test_the_fsm_literals_equal_the_contract(mirror: object, model: type, field: str) -> None:
    assert get_args(mirror) == get_args(model.model_fields[field].annotation)


def test_settings_carry_the_thresholds() -> None:
    settings = Settings()

    assert isinstance(settings, Thresholds)
    assert (
        settings.profile_sufficiency_min,
        settings.rediscovery_loop_limit,
        settings.low_confidence_streak_limit,
    ) == (0.7, 2, 2)


def test_the_diagram_draws_an_edge_for_every_state(capsys: pytest.CaptureFixture[str]) -> None:
    mermaid.main()
    out = capsys.readouterr().out
    edges = [line.split(":")[0].split() for line in out.splitlines() if " --> " in line]

    assert out.startswith("stateDiagram-v2\n")
    for state in FsmState:
        alias = mermaid.ALIAS[state]
        assert any(alias in (edge[0], edge[2]) for edge in edges), state
    assert "QO --> HO" not in out  # C13
