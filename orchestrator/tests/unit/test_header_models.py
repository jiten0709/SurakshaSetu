"""Audit headers are stored in clear and hashed into the chain, so they must hold nothing
personal. The walk splits field names on "_" and fails on any personal-looking token."""

import re
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest
from pydantic import BaseModel, ValidationError

import surakshasetu.audit
from surakshasetu.audit.events import HEADERS, ErasureRequestHeader, EventType, Header

BANNED = {
    "name",
    "age",
    "income",
    "salary",
    "dob",
    "birth",
    "gender",
    "occupation",
    "pincode",
    "address",
    "text",
    "utterance",
    "query",
    "phone",
    "mobile",
    "email",
    "pan",
    "aadhaar",
}


def field_names(model: type[BaseModel]) -> Iterator[str]:
    for name, field in model.model_fields.items():
        yield name
        for arg in (field.annotation, *get_args(field.annotation)):
            if isinstance(arg, type) and issubclass(arg, BaseModel):
                yield from field_names(arg)


def personal(model: type[BaseModel]) -> list[str]:
    return sorted(name for name in field_names(model) if BANNED & set(name.split("_")))


def test_every_event_type_has_a_header_model() -> None:
    assert set(HEADERS) == set(EventType)


@pytest.mark.parametrize("event_type", list(EventType))
def test_header_holds_no_personal_field(event_type: EventType) -> None:
    assert personal(HEADERS[event_type]) == []


def test_the_walk_catches_personal_names_even_when_nested() -> None:
    """Control: without it, a walk that saw no fields would pass every case above."""

    class Customer(BaseModel):
        full_name: str

    class Leaky(Header):
        customer: Customer | None = None
        age_years: int
        language: str  # contains "age", but not as a token

    assert personal(Leaky) == ["age_years", "full_name"]


def test_headers_refuse_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ErasureRequestHeader.model_validate({"reason_code": "CUSTOMER", "note": "x"})


def test_audit_code_never_updates_or_deletes() -> None:
    package = Path(surakshasetu.audit.__file__).parent
    offenders = [
        path.name
        for path in package.glob("*.py")
        if re.search(r"\b(UPDATE|DELETE|TRUNCATE)\b", path.read_text())
    ]
    assert offenders == []
