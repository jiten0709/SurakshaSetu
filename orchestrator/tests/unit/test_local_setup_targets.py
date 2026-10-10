"""`make local-setup` runs TDD §7.5 steps 1-6 in order (Step 24). Read from the Makefile's text:
running make here would start the stack."""

import re
from pathlib import Path

MAKEFILE = (Path(__file__).resolve().parents[3] / "Makefile").read_text("utf-8")

# TDD §7.5: each step and the targets that carry it, in order.
STEPS = {
    1: ["up"],  # compose, then db-migrate: Flyway and the checkpointer's tables
    2: ["seed-catalog"],  # catalog rows, disclosure sets and their hashes
    3: ["kb-ingest", "kb-verify"],  # the review gate, the snapshot rows, three collections
    4: ["seed-eval"],  # golden sets, red-team suite, scripted conversation
    5: ["test-invariants", "e2e-scripted"],  # the properties, the scripted conversation
    6: ["verify-audit", "verify-release-gate"],  # the chains; no DUMMY text released in pilot
}


def recipe(target: str) -> str:
    found = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n)+)", MAKEFILE, re.MULTILINE)
    assert found, f"no rule for {target}"
    return found.group(1)


def test_every_step_has_its_target() -> None:
    for targets in STEPS.values():
        for target in targets:
            recipe(target)
    assert "db-migrate" in recipe("up")


def test_local_setup_runs_the_steps_in_order_ending_with_step_6() -> None:
    loop = recipe("local-setup")
    listed = re.search(r"for t in (.*?); do", loop.replace("\\\n", " "), re.DOTALL)
    assert listed, loop
    words = listed.group(1).replace('"', " ").split()
    names = [word for word in words if re.fullmatch(r"[a-z][a-z0-9-]*", word)]
    wanted = [target for targets in STEPS.values() for target in targets]
    assert [n for n in names if n in wanted] == wanted
    assert names[-2:] == ["verify-audit", "verify-release-gate"]
    assert '"verify-audit DATE=$$(date -u +%F)"' in loop  # today, UTC


def test_nothing_is_a_placeholder_any_more() -> None:
    assert re.search(r"^PLACEHOLDERS :=\s*$", MAKEFILE, re.MULTILINE)
    assert "pytest" in recipe("verify-release-gate")
    assert "test_release_gate_pilot.py" in recipe("verify-release-gate")


def test_the_scripted_run_keeps_its_session_for_the_dossier() -> None:
    assert "SS_GOLDEN_KEEP" in recipe("e2e-scripted")
    assert "surakshasetu.audit.dossier" in recipe("dossier")
