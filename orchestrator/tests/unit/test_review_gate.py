"""The review gate (TDD §2.2: nothing is searchable until compliance approves it), the manifest,
and the seed corpus's front matter and DUMMY markers."""

import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from surakshasetu.kb.chunker import RawChunk
from surakshasetu.kb.manifest import (
    DocMeta,
    DocReview,
    Manifest,
    enrich,
    load_manifest,
    read_source,
    review_gate,
)
from surakshasetu.kb.payload import COLLECTIONS

KB = Path(__file__).parents[3] / "content" / "seed" / "kb"
SEED = load_manifest(KB / "review-manifest.yaml")
HEADING = re.compile(r"^#{2,6} ")


def doc(doc_id: str, status: str = "approved", **overrides: Any) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "collection": "product",
        "path": f"product/{doc_id}.md",
        "version": "v1",
        "effective_from": "2026-09-01",
        "effective_to": None,
        "status": status,
        "by": "DUMMY-product.compliance",
        "at": "2026-09-02",
    } | overrides


def manifest(*docs: dict[str, Any], **snapshots: str) -> Manifest:
    releases = snapshots or {"product": "product-2026-09-01"}
    return Manifest.model_validate(
        {
            "snapshots": {
                c: {"snapshot_id": s, "approved_by": "DUMMY-compliance"}
                for c, s in releases.items()
            },
            "documents": list(docs),
        }
    )


META = DocMeta(
    breadcrumb=["Suraksha Term Shield (999N001V02)", "Brochure v1"],
    citation_prefix="SurakshaTermShield_999N001V02_Brochure",
    doc_type="brochure",
    product_uin="999N001V02",
    product_types=["term"],
)


def payload(review: DocReview) -> Any:
    chunk = RawChunk("1", tuple(META.breadcrumb), "DUMMY: text")
    return enrich(review, META, chunk, snapshot_id="product-2026-09-01", uri_base="kb")


def test_only_approved_documents_pass_the_gate() -> None:
    m = manifest(doc("a"), doc("b", status="pending"))
    a, b = (DocReview.model_validate(d) for d in (doc("a"), doc("b", status="pending")))
    unknown = DocReview.model_validate(doc("c"))  # not in the manifest at all

    kept = review_gate([payload(a), payload(b), payload(unknown)], m)

    assert [p.doc_id for p in kept] == ["a"]


def test_enrich_builds_a_valid_payload_with_citation_and_source() -> None:
    p = payload(DocReview.model_validate(doc("999N001V02:br-v1", path="product/br.md")))

    assert p.citation_label == "SurakshaTermShield_999N001V02_Brochure §1"
    assert p.source_uri == "kb/product/br.md#1"
    assert p.chunk_id.startswith("product:999N001V02:br-v1:1:")
    assert p.review.status == "approved"


def test_front_matter_cannot_carry_another_collections_fields() -> None:
    meta = DocMeta.model_validate(META.model_dump(exclude_unset=True) | {"statute": "ITA2025"})
    chunk = RawChunk("1", ("x",), "DUMMY: text")

    with pytest.raises(ValueError, match="statute"):
        enrich(DocReview.model_validate(doc("a")), meta, chunk, snapshot_id="s", uri_base="kb")


@pytest.mark.parametrize(
    ("docs", "snapshots"),
    [
        ([doc("a"), doc("a", path="product/other.md")], {}),  # duplicate doc_id
        ([doc("a"), doc("b", path="product/a.md")], {}),  # duplicate path
        ([doc("a", path="tax/a.md")], {}),  # path outside its collection
        ([doc("a", collection="tax", path="tax/a.md")], {}),  # no tax snapshot release
        ([doc("a")], {"product": "tax-2026-09-01"}),  # snapshot id names another collection
        ([doc("a b")], {}),  # doc ids carry no spaces
    ],
)
def test_inconsistent_manifests_are_refused(
    docs: list[dict[str, Any]], snapshots: dict[str, str]
) -> None:
    with pytest.raises(ValidationError):
        manifest(*docs, **snapshots)


# --- the seed corpus --------------------------------------------------------------------------
def test_the_seed_manifest_releases_all_three_collections_and_holds_a_pending_doc() -> None:
    assert set(SEED.snapshots) == set(COLLECTIONS)
    assert {d.collection for d in SEED.documents} == set(COLLECTIONS)
    assert any(d.status == "pending" for d in SEED.documents)


@pytest.mark.parametrize("review", SEED.documents, ids=lambda d: d.doc_id)
def test_every_seed_document_is_complete_and_dummy(review: DocReview) -> None:
    meta, body = read_source(KB / review.path)
    # Front matter complete for its collection: one synthetic chunk validates as a full payload.
    chunk = RawChunk("1", tuple(meta.breadcrumb), "DUMMY: probe")
    enrich(
        review,
        meta,
        chunk,
        snapshot_id=SEED.snapshots[review.collection].snapshot_id,
        uri_base="kb",
    )
    # Every section's first line starts with DUMMY.
    lines = [line for line in body.splitlines() if line.strip()]
    assert lines, review.path
    for heading, first in zip(lines, lines[1:], strict=False):
        if HEADING.match(heading):
            assert first.startswith("DUMMY:"), f"{review.path}: {heading}"
    assert not HEADING.match(lines[-1]), f"{review.path}: ends with an empty section"
