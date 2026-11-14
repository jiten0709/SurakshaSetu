"""O1 (readiness §14.1): the adapter sends a title-only subschema as `{}`. Nothing else in a schema
changes, and the normalised TurnAnalysis schema accepts and refuses exactly what the original does
on the schema probe's instances: every scripted nlu-extract reply in the goldens and the red-team
suite, plus the probe's invalid variants."""

import copy
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from surakshasetu.analysis.models import TurnAnalysis
from surakshasetu.gateway.adapter import _normalise_schema

CONTENT = Path(__file__).resolve().parents[3] / "content"
# tests/golden/harness.py's ANALYSIS: a scripted nlu-extract reply names only what it needs.
ANALYSIS = {"intents": [], "slots": [], "side_query": None, "language": "en"}


def test_turn_analysis_changes_only_the_untyped_slot_value() -> None:
    schema = TurnAnalysis.model_json_schema()
    expected = copy.deepcopy(schema)
    assert expected["$defs"]["SlotCandidate"]["properties"]["value"] == {"title": "Value"}
    expected["$defs"]["SlotCandidate"]["properties"]["value"] = {}

    assert _normalise_schema(schema) == expected
    assert schema["$defs"]["SlotCandidate"]["properties"]["value"] == {"title": "Value"}  # a copy


def test_only_schema_positions_are_normalised() -> None:
    named_title = {"type": "object", "properties": {"title": {"type": "string", "title": "Title"}}}
    untyped_title = {"properties": {"title": {"title": "Title"}}}
    annotated = {"title": "Note", "description": "free text"}
    lookalikes = {"type": "object", "default": {"title": "x"}, "examples": [{"title": "y"}]}
    nested = {"anyOf": [{"title": "A"}, {"type": "null"}], "items": {"title": "B"}}

    assert _normalise_schema(named_title) == named_title
    assert _normalise_schema(untyped_title) == {"properties": {"title": {}}}
    assert _normalise_schema(annotated) == annotated
    assert _normalise_schema(lookalikes) == lookalikes
    assert _normalise_schema(nested) == {"anyOf": [{}, {"type": "null"}], "items": {}}
    assert _normalise_schema(True) is True


def _scripted_replies() -> list[Any]:
    found: list[Any] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "nlu-extract" and isinstance(value, list):
                    found.extend(
                        r
                        for r in value
                        if isinstance(r, dict) and "content" not in r and "status" not in r
                    )
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    files = sorted((CONTENT / "golden").rglob("*.yaml")) + sorted(
        (CONTENT / "redteam").glob("*.yaml")
    )
    for path in files:
        walk(yaml.safe_load(path.read_text("utf-8")))
    return found


def _instances() -> list[Any]:
    """As the probe built them (evidence 2026-11-13-schema-probe, scripts/schema_probe.py)."""
    completed: list[Any] = []
    for reply in _scripted_replies():
        try:
            completed.append(TurnAnalysis.model_validate(ANALYSIS | reply).model_dump(mode="json"))
        except ValidationError:
            completed.append(ANALYSIS | reply)  # kept as-is: both schemas must still agree on it
    base = completed[0]
    slot = {"slot": "age_years", "value": 34, "confidence": 0.9, "evidence_span": "34"}
    invalid = [
        base | {"intents": ["NOT_AN_INTENT"]},
        {k: v for k, v in base.items() if k != "language"},
        {k: v for k, v in base.items() if k != "intents"},
        base | {"language": "fr"},
        base | {"side_query": 5},
        base | {"slots": "none"},
        base | {"slots": [slot | {"confidence": "high"}]},
        base | {"slots": [{k: v for k, v in slot.items() if k != "value"}]},
        base | {"slots": [slot | {"value": {"nested": [1, 2]}}]},  # valid: value is any
        base | {"slots": [slot | {"value": None}]},  # valid: value is any
        base | {"intents": "SLOT_ANSWER"},
        "not an object",
    ]
    return completed + invalid


def test_the_normalised_schema_gives_the_same_verdict_on_every_probe_instance() -> None:
    schema = TurnAnalysis.model_json_schema()
    original = Draft202012Validator(schema)
    normalised = Draft202012Validator(_normalise_schema(schema))
    instances = _instances()

    verdicts = [(original.is_valid(i), normalised.is_valid(i)) for i in instances]

    assert len(instances) >= 200  # 200 on the probe's date; the goldens only grow
    assert all(a == b for a, b in verdicts)
    assert {a for a, _ in verdicts} == {True, False}  # both outcomes are exercised
