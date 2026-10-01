"""The Qdrant payload of one knowledge-base chunk (TDD §2.3, in the §7.4 seed format) and its
deterministic ids. Step 12 filters and cites from these fields; kb-verify validates every point."""

import hashlib
import unicodedata
from datetime import date
from typing import Annotated, Literal, Self
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

Collection = Literal["regulatory", "product", "tax"]
COLLECTIONS: tuple[Collection, ...] = ("regulatory", "product", "tax")
# uuid5(NAMESPACE_URL, "urn:surakshasetu:kb"). Fixed forever: changing it changes every point id.
KB_NAMESPACE = UUID("e69f5201-7ad0-5d17-8f8d-d6e1fbe10d0a")

Uin = Annotated[str, Field(pattern=r"^\d{3}[NA]\d{3}V\d{2}$")]
TaxYear = Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")]


def content_sha256(text: str) -> str:
    return hashlib.sha256(unicodedata.normalize("NFC", text).encode()).hexdigest()


def chunk_id(domain: str, doc_id: str, section_id: str, sha256: str) -> str:
    return f"{domain}:{doc_id}:{section_id}:{sha256[:6]}"


def point_id(snapshot_id: str, chunk: str) -> UUID:
    """Per snapshot, so an unchanged chunk in a newer snapshot never overwrites an older pin."""
    return uuid5(KB_NAMESPACE, f"{snapshot_id}/{chunk}")


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["approved", "pending"]
    by: str
    at: date


class _Payload(BaseModel):
    """Every key is required, nullable ones included, so a stored payload is complete."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk_id: str
    domain: Collection  # narrowed to one literal by each subclass: the discriminator
    doc_id: str
    doc_title: (
        str  # the breadcrumb root, e.g. "Suraksha Term Shield (999N001V02) › Policy Wording v2"
    )
    version: str
    doc_type: str
    section_id: str
    section_path: list[str] = Field(min_length=1)
    citation_label: str
    snapshot_id: str
    product_uin: Uin | None
    product_types: list[str]
    effective_from: date
    effective_to: date | None
    status: Literal["in_force", "withdrawn"]
    supersedes: str | None
    jurisdiction: Literal["IN"]
    language: str
    source_uri: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    review: Review
    sensitivity: Literal["public", "internal", "restricted"]
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ids_match_the_text(self) -> Self:
        if self.content_sha256 != content_sha256(self.text):
            raise ValueError("content_sha256 does not match text")
        if self.chunk_id != chunk_id(
            self.domain, self.doc_id, self.section_id, self.content_sha256
        ):
            raise ValueError("chunk_id does not match domain, doc_id, section_id and hash")
        return self


class RegulatoryPayload(_Payload):
    domain: Literal["regulatory"]
    instrument: Literal[
        "regulation",
        "master_circular",
        "circular",
        "exposure_draft",
        "act",
        "rules",
        "board_policy",
        "notice",
    ]
    reference_no: str | None
    issued_on: date | None
    applies_to: list[Literal["life", "linked", "non_linked", "par", "non_par"]]
    superseded_by: str | None


class ProductPayload(_Payload):
    domain: Literal["product"]
    product_uin: Uin


class TaxPayload(_Payload):
    domain: Literal["tax"]
    statute: Literal["ITA1961", "ITA2025"]
    section_aliases: list[str]
    tax_years: list[TaxYear] = Field(min_length=1)
    regime: Literal["old", "new", "both"]


KbPayload = Annotated[
    RegulatoryPayload | ProductPayload | TaxPayload, Field(discriminator="domain")
]
PAYLOAD: TypeAdapter[KbPayload] = TypeAdapter(KbPayload)


def indexed_text(payload: KbPayload) -> str:
    """What is embedded, BM25-indexed and reranked: the breadcrumb, then the text (TDD §2.2)."""
    return " › ".join(payload.section_path) + "\n\n" + payload.text
