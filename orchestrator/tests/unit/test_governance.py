"""Change governance (TDD §4.5, §5.4; Step 23): CODEOWNERS covers every governed artefact and names
only paths that exist, and no experiment config refers to what is never A/B tested: consent copy,
disclosures, suitability rules or ranking weights."""

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
CODEOWNERS = REPO / "CODEOWNERS"
EXPERIMENTS = REPO / "content" / "experiments"

# The §4.5 artefacts and where they live; each must have an owner.
GOVERNED = (
    "/content/prompt-bundles/",
    "/content/lexicon/",
    "/content/seed/catalog/",
    "/content/seed/kb/",
    "/domain-services/src/main/resources/dmn/",
    "/domain-services/src/main/resources/params/",
    "/domain-services/src/main/resources/ranking/",
    "/domain-services/src/main/resources/rating/",
    "/infra/omniroute/",
)
# What an experiment may never touch (TDD §5.4), by path, id or key.
NEVER_TESTED = re.compile(
    r"seed/consent|notices?\b|consent[_-]|greeting|age_confirm_ask"  # consent copy and the notice
    r"|disclosure|DISC-|registry"  # disclosures
    r"|suitability|\.dmn|\bdmn\b|actuarial|params/"  # suitability rules and parameters
    r"|ranking|weights",  # ranking weights
    re.IGNORECASE,
)


def rules() -> list[tuple[str, list[str]]]:
    lines = [line.strip() for line in CODEOWNERS.read_text(encoding="utf-8").splitlines()]
    return [
        (line.split()[0], line.split()[1:]) for line in lines if line and not line.startswith("#")
    ]


def test_every_codeowners_path_exists_and_has_an_owner() -> None:
    for path, owners in rules():
        assert (REPO / path.lstrip("/")).exists(), path
        assert owners and all(re.fullmatch(r"@[\w.-]+(/[\w.-]+)?", o) for o in owners), path


def test_every_governed_artefact_has_an_owner() -> None:
    assert set(GOVERNED) <= {path for path, _ in rules()}


def references(root: Path) -> dict[str, list[str]]:
    """Each experiment config's references to what is never A/B tested (comments included: a
    config must not even point at them)."""
    found = {}
    for path in sorted(root.rglob("*.y*ml")):
        hits = sorted({m.group(0) for m in NEVER_TESTED.finditer(path.read_text(encoding="utf-8"))})
        if hits:
            found[path.name] = hits
    return found


def test_no_experiment_touches_consent_disclosures_suitability_or_ranking() -> None:
    assert list(EXPERIMENTS.glob("*.yaml")), "an experiment config to check"
    assert references(EXPERIMENTS) == {}


@pytest.mark.parametrize(
    "target",
    [
        "consent_reprompt",
        "greeting",
        "notice:2026.09.1-en",
        "DISC-GLOBAL-AI-06",
        "registry:DISC-GLOBAL-TAX-05",
        "dmn/suitability-2026.09.1.dmn",
        "params/actuarial-2026.09.1.yaml",
        "ranking/weights-2026.09.1.yaml",
    ],
)
def test_a_planted_reference_is_caught(tmp_path: Path, target: str) -> None:
    (tmp_path / "exp.yaml").write_text(f"id: exp-x\ntargets: [{target}]\n", encoding="utf-8")
    assert references(tmp_path)


def test_wording_experiments_pass(tmp_path: Path) -> None:
    (tmp_path / "exp.yaml").write_text(
        "id: exp-x\ntargets: [side_query_bridge, RL-S2-INCOME]\n", encoding="utf-8"
    )
    assert references(tmp_path) == {}
