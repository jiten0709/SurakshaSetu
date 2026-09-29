"""The chunk payload (TDD §2.3 and §7.4) and its deterministic ids."""

from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from surakshasetu.kb.payload import (
    KB_NAMESPACE,
    PAYLOAD,
    ProductPayload,
    RegulatoryPayload,
    TaxPayload,
    chunk_id,
    content_sha256,
    point_id,
)

TEXT = "DUMMY: On the death of the life assured during the policy term, the death benefit is paid."
SHA = content_sha256(TEXT)


def product(**overrides: Any) -> dict[str, Any]:
    payload = {
        "chunk_id": chunk_id("product", "999N001V02:pw-v2", "2.4", SHA),
        "domain": "product",
        "doc_id": "999N001V02:pw-v2",
        "doc_title": "Suraksha Term Shield (999N001V02) › Policy Wording v2",
        "version": "v2",
        "doc_type": "policy_wording",
        "section_id": "2.4",
        "section_path": [
            "Suraksha Term Shield (999N001V02)",
            "Policy Wording v2",
            "2. Benefits",
            "2.4 Death benefit",
        ],
        "citation_label": "SurakshaTermShield_999N001V02_PolicyWording §2.4",
        "snapshot_id": "product-2026-09-01",
        "product_uin": "999N001V02",
        "product_types": ["term", "non_linked", "non_par"],
        "effective_from": "2026-09-01",
        "effective_to": None,
        "status": "in_force",
        "supersedes": None,
        "jurisdiction": "IN",
        "language": "en",
        "source_uri": "content/seed/kb/product/999N001V02-policy-wording-v2.md#2.4",
        "content_sha256": SHA,
        "review": {"status": "approved", "by": "DUMMY-product.compliance", "at": "2026-09-02"},
        "sensitivity": "public",
        "text": TEXT,
    }
    return payload | overrides


def regulatory(**overrides: Any) -> dict[str, Any]:
    base = product(
        chunk_id=chunk_id("regulatory", "irdai-ppi-mc-2024", "3.2", SHA),
        domain="regulatory",
        doc_id="irdai-ppi-mc-2024",
        doc_type="master_circular",
        section_id="3.2",
        product_uin=None,
        product_types=[],
        instrument="master_circular",
        reference_no="DUMMY/IRDAI/MC/2024/01",
        issued_on="2024-09-05",
        applies_to=["life"],
        superseded_by=None,
    )
    return base | overrides


def tax(**overrides: Any) -> dict[str, Any]:
    base = product(
        chunk_id=chunk_id("tax", "ita2025-s123", "123(1)", SHA),
        domain="tax",
        doc_id="ita2025-s123",
        doc_type="statute",
        section_id="123(1)",
        product_uin=None,
        product_types=[],
        statute="ITA2025",
        section_aliases=["123", "80C", "Sch XV para 1(a)"],
        tax_years=["2026-27"],
        regime="old",
    )
    return base | overrides


@pytest.mark.parametrize(
    ("payload", "model"),
    [(product(), ProductPayload), (regulatory(), RegulatoryPayload), (tax(), TaxPayload)],
)
def test_each_domain_validates_to_its_own_model(payload: dict[str, Any], model: type) -> None:
    parsed = PAYLOAD.validate_python(payload)

    assert type(parsed) is model
    assert PAYLOAD.validate_python(parsed.model_dump(mode="json")) == parsed  # a Qdrant round trip


@pytest.mark.parametrize(
    "payload",
    [
        product(unexpected="x"),
        {k: v for k, v in product().items() if k != "effective_to"},  # every key present
        product(section_path="2. Benefits > 2.4"),  # an array, not a string
        product(product_uin=None),  # a product chunk names its UIN
        product(domain="claims"),
        product(text="DUMMY: tampered"),  # content_sha256 no longer matches
        product(chunk_id="product:999N001V02:pw-v2:2.4:000000"),
        regulatory(instrument=None),
        regulatory(applies_to=["motor"]),
        tax(tax_years=["2026"]),
        tax(regime="any"),
        tax(statute="ITA1922"),
    ],
)
def test_malformed_payloads_are_refused(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PAYLOAD.validate_python(payload)


def test_chunk_id_is_domain_doc_section_and_hash_prefix() -> None:
    assert chunk_id("product", "999N001V02:pw-v2", "5.3", "9f1c2a" + "0" * 58) == (
        "product:999N001V02:pw-v2:5.3:9f1c2a"
    )


def test_content_hash_is_sha256_of_nfc_utf8() -> None:
    assert content_sha256("é") == content_sha256("é")  # composed and decomposed
    assert content_sha256("abc") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
    )


def test_point_ids_are_uuid5_of_snapshot_and_chunk() -> None:
    cid = product()["chunk_id"]

    assert UUID("e69f5201-7ad0-5d17-8f8d-d6e1fbe10d0a") == KB_NAMESPACE
    assert point_id("product-2026-09-01", cid) == point_id("product-2026-09-01", cid)
    assert point_id("product-2026-09-01", cid).version == 5
    # The same chunk in a later snapshot is a separate point, so the older pin keeps its own.
    assert point_id("product-2026-09-01", cid) != point_id("product-2026-10-01", cid)
