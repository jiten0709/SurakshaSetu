"""The committed models.py round-trips a sample payload for every object schema in the contract."""

from pathlib import Path
from typing import Any

import pytest
from openapi_spec_validator import validate
from openapi_spec_validator.readers import read_from_filename
from pydantic import BaseModel, ValidationError

from surakshasetu.domain import models

SPEC_PATH = Path(__file__).parents[3] / "contracts" / "openapi" / "domain-services.v1.yaml"
SPEC, _ = read_from_filename(str(SPEC_PATH))
SCHEMAS: dict[str, Any] = SPEC["components"]["schemas"]

# A valid value for each pattern the contract uses.
PATTERN_SAMPLES = {
    SCHEMAS["Money"]["pattern"]: "1234.50",
    SCHEMAS["SignedMoney"]["pattern"]: "-150000.50",
    SCHEMAS["Sha256Hex"]["pattern"]: "ab" * 32,
    SCHEMAS["Uin"]["pattern"]: "999N001V02",
    SCHEMAS["Pincode"]["pattern"]: "411001",
}
FORMAT_SAMPLES = {
    "uuid": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b",
    "date": "2026-09-23",
    "date-time": "2026-09-23T10:15:00Z",
}


def sample(schema: dict[str, Any]) -> Any:
    """A payload with every property present, so nothing can be dropped silently."""
    if "$ref" in schema:
        return sample(SCHEMAS[schema["$ref"].rsplit("/", 1)[1]])
    if "allOf" in schema:
        parts = [sample(part) for part in schema["allOf"]]
        return parts[0] if len(parts) == 1 else {k: v for part in parts for k, v in part.items()}
    if "anyOf" in schema:
        return sample(next(s for s in schema["anyOf"] if s.get("type") != "null"))
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema["type"]
    if isinstance(kind, list):
        kind = next(k for k in kind if k != "null")
    if kind == "object":
        if "properties" in schema:
            return {name: sample(prop) for name, prop in schema["properties"].items()}
        key = sample(schema["propertyNames"]) if "propertyNames" in schema else "key"
        return {key: sample(schema["additionalProperties"])}
    if kind == "array":
        return [sample(schema["items"])]
    if kind == "string":
        return PATTERN_SAMPLES.get(
            schema.get("pattern"), FORMAT_SAMPLES.get(schema.get("format"), "x")
        )
    if kind == "integer":
        return schema.get("minimum", 1)
    if kind == "number":
        return schema.get("maximum", 0.5)
    assert kind == "boolean", kind
    return True


OBJECT_SCHEMAS = sorted(
    name for name, schema in SCHEMAS.items() if "properties" in schema or "allOf" in schema
)


def test_spec_is_valid_openapi() -> None:
    validate(SPEC)


@pytest.mark.parametrize("name", OBJECT_SCHEMAS)
def test_round_trip(name: str) -> None:
    model: type[BaseModel] = getattr(models, name)
    payload = sample(SCHEMAS[name])

    assert model.model_validate(payload).model_dump(mode="json", exclude_unset=True) == payload


@pytest.mark.parametrize("bad", ["12.345", "1,000", "-5", "1e5", ""])
def test_money_is_a_non_negative_decimal_string(bad: str) -> None:
    with pytest.raises(ValidationError):
        models.Liability(kind="home", outstanding_inr=bad, years_left=5)


def test_consent_method_uses_the_ddl_spelling() -> None:
    payload = sample(SCHEMAS["ConsentRecordCreate"])
    assert models.ConsentRecordCreate.model_validate(payload | {"method": "parsed_affirmation"})
    with pytest.raises(ValidationError):
        models.ConsentRecordCreate.model_validate(payload | {"method": "typed_affirmation"})


def test_declined_answers_are_explicit_nulls() -> None:
    payload = sample(SCHEMAS["EligibilityRequest"]) | {"tobacco_12m": None, "occupation_code": None}
    request = models.EligibilityRequest.model_validate(payload)
    assert request.model_dump(mode="json", exclude_unset=True)["tobacco_12m"] is None

    del payload["tobacco_12m"]
    with pytest.raises(ValidationError):
        models.EligibilityRequest.model_validate(payload)
