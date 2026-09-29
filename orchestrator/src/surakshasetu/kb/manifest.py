"""The compliance review record (content/seed/kb/review-manifest.yaml), each source document's
front matter, and the review gate: nothing is searchable until compliance approves it (TDD §2.2)."""

import logging
from collections import Counter
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from surakshasetu.kb.chunker import RawChunk
from surakshasetu.kb.payload import PAYLOAD, Collection, KbPayload, Review, chunk_id, content_sha256

logger = logging.getLogger(__name__)

_EXTRAS: dict[str, frozenset[str]] = {
    "regulatory": frozenset(
        {"instrument", "reference_no", "issued_on", "applies_to", "superseded_by"}
    ),
    "product": frozenset(),
    "tax": frozenset({"statute", "section_aliases", "tax_years", "regime"}),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SnapshotRelease(_Strict):
    """A collection release: the snapshot id sessions pin, and who approved it."""

    snapshot_id: str
    approved_by: str


class DocReview(_Strict):
    doc_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.:_-]*$")
    collection: Collection
    path: str  # relative to content/seed/kb
    version: str
    effective_from: date
    effective_to: date | None
    status: Literal["approved", "pending"]
    by: str
    at: date


class Manifest(_Strict):
    snapshots: dict[Collection, SnapshotRelease]
    documents: list[DocReview] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        for key in ("doc_id", "path"):
            dupes = [
                v for v, n in Counter(getattr(d, key) for d in self.documents).items() if n > 1
            ]
            if dupes:
                raise ValueError(f"duplicate {key}: {', '.join(dupes)}")
        for doc in self.documents:
            if not doc.path.startswith(f"{doc.collection}/"):
                raise ValueError(f"{doc.doc_id}: path must be under {doc.collection}/")
            if doc.collection not in self.snapshots:
                raise ValueError(f"{doc.doc_id}: no snapshot release for {doc.collection}")
        for collection, release in self.snapshots.items():
            if not release.snapshot_id.startswith(f"{collection}-"):
                raise ValueError(f"snapshot {release.snapshot_id} must start with {collection}-")
        return self


class DocMeta(_Strict):
    """A source document's YAML front matter: what it is, not whether it is approved."""

    breadcrumb: list[str] = Field(min_length=1)  # the chunks' breadcrumb root
    citation_prefix: str  # citation_label = f"{citation_prefix} §{section_id}"
    doc_type: str
    language: str = "en"
    sensitivity: Literal["public", "internal", "restricted"] = "public"
    status: Literal["in_force", "withdrawn"] = "in_force"
    supersedes: str | None = None
    product_uin: str | None = None
    product_types: list[str] = []
    # regulatory
    instrument: str | None = None
    reference_no: str | None = None
    issued_on: date | None = None
    applies_to: list[str] = []
    superseded_by: str | None = None
    # tax
    statute: str | None = None
    section_aliases: list[str] = []
    tax_years: list[str] = []
    regime: str | None = None


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def read_source(path: Path) -> tuple[DocMeta, str]:
    """Split a source document into its front matter and its Markdown body."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name}: no front matter")
    _, front, body = text.split("---\n", 2)
    return DocMeta.model_validate(yaml.safe_load(front)), body


def enrich(
    doc: DocReview, meta: DocMeta, chunk: RawChunk, *, snapshot_id: str, uri_base: str
) -> KbPayload:
    """The full payload of one chunk. Fails if the front matter sets another collection's fields."""
    foreign = meta.model_fields_set & (
        frozenset().union(*_EXTRAS.values()) - _EXTRAS[doc.collection]
    )
    if foreign:
        raise ValueError(
            f"{doc.doc_id}: {', '.join(sorted(foreign))} not allowed in {doc.collection}"
        )
    sha = content_sha256(chunk.text)
    return PAYLOAD.validate_python(
        {
            "chunk_id": chunk_id(doc.collection, doc.doc_id, chunk.section_id, sha),
            "domain": doc.collection,
            "doc_id": doc.doc_id,
            "doc_title": " › ".join(meta.breadcrumb),
            "version": doc.version,
            "doc_type": meta.doc_type,
            "section_id": chunk.section_id,
            "section_path": list(chunk.section_path),
            "citation_label": f"{meta.citation_prefix} §{chunk.section_id}",
            "snapshot_id": snapshot_id,
            "product_uin": meta.product_uin,
            "product_types": meta.product_types,
            "effective_from": doc.effective_from,
            "effective_to": doc.effective_to,
            "status": meta.status,
            "supersedes": meta.supersedes,
            "jurisdiction": "IN",
            "language": meta.language,
            "source_uri": f"{uri_base}/{doc.path}#{chunk.section_id}",
            "content_sha256": sha,
            "review": Review(status=doc.status, by=doc.by, at=doc.at),
            "sensitivity": meta.sensitivity,
            "text": chunk.text,
            **meta.model_dump(include=set(_EXTRAS[doc.collection])),
        }
    )


def review_gate(payloads: Sequence[KbPayload], manifest: Manifest) -> list[KbPayload]:
    """Keep only chunks of documents the manifest records as approved."""
    approved = {d.doc_id for d in manifest.documents if d.status == "approved"}
    dropped = Counter(p.doc_id for p in payloads if p.doc_id not in approved)
    for doc_id, count in sorted(dropped.items()):
        logger.info("review gate: dropped %d chunks of %s (not approved)", count, doc_id)
    return [p for p in payloads if p.doc_id in approved]
