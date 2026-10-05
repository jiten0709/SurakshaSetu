"""I2's slot hash in Python is the Java tier's (Step 20): content/testvectors/needs, which
`Jcs.needsSha256` checks since Step 6, hashed by `s2.needs_sha256`, over exactly the JSON the
DomainClient sends (exclude_unset), minus slots_sha256, under RFC 8785 JCS."""

import json
from pathlib import Path
from typing import Any

import pytest

from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.domain.models import NeedsPayload, SuitabilityRequest
from surakshasetu.graph.states.s2 import needs_sha256

VECTORS = sorted(
    (Path(__file__).resolve().parents[3] / "content" / "testvectors" / "needs").glob("*.json")
)
BASE: dict[str, Any] = {
    "goals": ["income_protection"],
    "annual_income_inr": "1200000",
    "income_type": "salaried",
    "existing_annual_premium_inr": "0",
    "financial_distress": False,
    "comprehension_difficulty_count": 0,
}


def vector(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return loaded


def test_the_java_vectors_are_there() -> None:
    assert [p.stem for p in VECTORS] == ["01-tdd-example", "02-short-path", "03-income-declined"]


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_python_hashes_each_java_vector_to_its_inputs_sha256(path: Path) -> None:
    v = vector(path)
    assert needs_sha256(NeedsPayload.model_validate(v["needs"])) == v["inputs_sha256"]


@pytest.mark.parametrize("path", VECTORS, ids=lambda p: p.stem)
def test_the_hash_covers_exactly_what_the_client_sends(path: Path) -> None:
    """The DomainClient dumps the request with exclude_unset; its needs, minus slots_sha256, is
    the preimage the Suitability Service hashes ("exactly as sent")."""
    v = vector(path)
    request = SuitabilityRequest.model_validate(
        {
            "pins": {"rules": "rules-2026.09.1"},
            "eligibility": {
                "age_years": 34,
                "tobacco_12m": False,
                "eligible_uins": [],
                "flags": [],
            },
            "needs": v["needs"],
        }
    )
    sent = request.model_dump(mode="json", exclude_unset=True)["needs"]
    sent.pop("slots_sha256", None)
    assert sent == {k: val for k, val in v["needs"].items() if k != "slots_sha256"}
    assert sha256_hex(sent) == v["inputs_sha256"]


def test_slots_sha256_is_left_out_of_its_own_preimage() -> None:
    needs = NeedsPayload.model_validate(BASE)
    bound = needs.model_copy(update={"slots_sha256": needs_sha256(needs)})
    assert needs_sha256(bound) == needs_sha256(needs)


def test_a_slot_left_out_hashes_differently_from_its_default_sent() -> None:
    """Omitted means not asked (it takes its default and does not count toward sufficiency); a
    default sent means answered. The hash keeps them apart."""
    omitted = NeedsPayload.model_validate(BASE)
    sent = NeedsPayload.model_validate(BASE | {"existing_cover_inr": "0"})
    assert omitted.existing_cover_inr == sent.existing_cover_inr == "0"
    assert needs_sha256(omitted) != needs_sha256(sent)


def test_a_declined_null_stays_in_the_preimage() -> None:
    declined = NeedsPayload.model_validate(BASE | {"earmarked_assets_inr": None})
    assert needs_sha256(declined) != needs_sha256(NeedsPayload.model_validate(BASE))
