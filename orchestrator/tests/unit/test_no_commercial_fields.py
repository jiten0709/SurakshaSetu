"""Commission, margin and incentives are never inputs to ranking or suitability (TDD §3.8)."""

import re
from pathlib import Path
from typing import Any

from openapi_spec_validator.readers import read_from_filename

SPEC, _ = read_from_filename(
    str(Path(__file__).parents[3] / "contracts" / "openapi" / "domain-services.v1.yaml")
)
BANNED = re.compile(r"commission|margin|incentive|campaign|payout", re.IGNORECASE)


def names(node: Any) -> set[str]:
    """Every schema, property and parameter name and every enum value, at any depth."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("properties", "schemas"):
                found |= set(value)
            elif key == "enum":
                found |= {str(v) for v in value}
            elif key == "parameters":
                found |= {p["name"] for p in value if "name" in p}
            found |= names(value)
    elif isinstance(node, list):
        for item in node:
            found |= names(item)
    return found


def test_walk_reaches_nested_fields() -> None:
    assert {"sum_assured_inr", "outstanding_inr", "pins.rules", "P1_NEEDS_RECO"} <= names(SPEC)


def test_contract_has_no_commercial_fields() -> None:
    assert sorted(n for n in names(SPEC) if BANNED.search(n)) == []
