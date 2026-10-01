"""Shared by the retrieval unit tests: payload builders, a scripted TEI, and an in-process Qdrant
(qdrant-client's ":memory:" mode, which applies real filter, fusion and IDF semantics without a
network)."""

import shutil
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from qdrant_client import AsyncQdrantClient, models

from surakshasetu.gateway import GatewayUnavailable, TeiModel
from surakshasetu.kb.payload import (
    PAYLOAD,
    Collection,
    KbPayload,
    chunk_id,
    content_sha256,
    indexed_text,
    point_id,
)
from surakshasetu.retrieval import bm25
from surakshasetu.retrieval.rewrite import KB_CONFIG
from surakshasetu.retrieval.service import (
    RetrievalContext,
    RetrievalService,
    SnapshotMeta,
)

EMBED_MODEL = TeiModel("org/embedder", "e1")
RERANK_MODEL = TeiModel("org/reranker", "r1")
PINS: dict[Collection, str] = {
    "regulatory": "regulatory-test",
    "product": "product-test",
    "tax": "tax-test",
}
AS_OF = datetime(2026, 9, 29, 6, 0, tzinfo=UTC)
DIM = 4


def payload(
    domain: Collection,
    doc_id: str,
    section_path: list[str],
    text: str,
    *,
    root: int = 2,
    doc_type: str | None = None,
    product_uin: str | None = None,
    snapshot_id: str | None = None,
    effective_from: date = date(2026, 4, 1),
    effective_to: date | None = None,
    regime: Literal["old", "new", "both"] = "both",
    tax_years: list[str] | None = None,
) -> KbPayload:
    """A valid payload; section_id is the first word of the last heading."""
    section = section_path[-1].split()[0].rstrip(".")
    sha = content_sha256(text)
    raw: dict[str, Any] = {
        "chunk_id": chunk_id(domain, doc_id, section, sha),
        "domain": domain,
        "doc_id": doc_id,
        "doc_title": " › ".join(section_path[:root]),
        "version": "v1",
        "doc_type": doc_type
        or {"regulatory": "master_circular", "product": "policy_wording", "tax": "statute"}[domain],
        "section_id": section,
        "section_path": section_path,
        "citation_label": f"{doc_id} §{section}",
        "snapshot_id": snapshot_id or PINS[domain],
        "product_uin": product_uin or ("999N001V02" if domain == "product" else None),
        "product_types": [],
        "effective_from": effective_from.isoformat(),
        "effective_to": effective_to.isoformat() if effective_to else None,
        "status": "in_force",
        "supersedes": None,
        "jurisdiction": "IN",
        "language": "en",
        "source_uri": f"content/seed/kb/{doc_id}.md#{section}",
        "content_sha256": sha,
        "review": {"status": "approved", "by": "compliance", "at": "2026-09-22"},
        "sensitivity": "public",
        "text": text,
    }
    if domain == "regulatory":
        raw |= {
            "instrument": "master_circular",
            "reference_no": None,
            "issued_on": None,
            "applies_to": ["life"],
            "superseded_by": None,
        }
    if domain == "tax":
        raw |= {
            "statute": "ITA2025",
            "section_aliases": [],
            "tax_years": tax_years or ["2026-27"],
            "regime": regime,
        }
    return PAYLOAD.validate_python(raw)


class FakeTei:
    """Scores a document by its chunk text; every other call answers from what it was given."""

    def __init__(
        self,
        scores: Mapping[str, float] | None = None,
        *,
        embed_down: bool = False,
        rerank_down: bool = False,
        embed_model: TeiModel = EMBED_MODEL,
        rerank_model: TeiModel = RERANK_MODEL,
    ) -> None:
        self.scores = dict(scores or {})
        self.embed_down = embed_down
        self.rerank_down = rerank_down
        self.served_embed = embed_model
        self.served_rerank = rerank_model
        self.embedded: list[str] = []
        self.reranked: list[tuple[str, list[str]]] = []

    async def embed(
        self, texts: list[str], *, kind: Literal["query", "document"]
    ) -> list[list[float]]:
        if self.embed_down:
            raise GatewayUnavailable("UNAVAILABLE")
        self.embedded += texts
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    async def embed_model(self) -> TeiModel:
        if self.embed_down:
            raise GatewayUnavailable("UNAVAILABLE")
        return self.served_embed

    async def rerank(self, query: str, docs: list[str]) -> list[float]:
        if self.rerank_down:
            raise GatewayUnavailable("TIMEOUT")
        self.reranked.append((query, docs))
        return [self.scores.get(doc.split("\n\n", 1)[1], 0.0) for doc in docs]

    async def rerank_model(self) -> TeiModel:
        if self.rerank_down:
            raise GatewayUnavailable("UNAVAILABLE")
        return self.served_rerank


async def qdrant_with(
    payloads: list[KbPayload], vectors: Mapping[str, list[float]] | None = None
) -> AsyncQdrantClient:
    """kb_<domain> collections as Step 11 builds them: dense cosine, sparse bm25 with IDF. Every
    dense vector is alike unless `vectors` (by chunk text) says otherwise."""
    client = AsyncQdrantClient(location=":memory:")
    for domain in ("regulatory", "product", "tax"):
        await client.create_collection(
            f"kb_{domain}",
            vectors_config={
                "dense": models.VectorParams(size=DIM, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    for p in payloads:
        sparse = bm25.doc_vector(bm25.tokenize(indexed_text(p)), avgdl=40.0)
        await client.upsert(
            f"kb_{p.domain}",
            points=[
                models.PointStruct(
                    id=str(point_id(p.snapshot_id, p.chunk_id)),
                    vector={
                        "dense": (vectors or {}).get(p.text, [0.1, 1.0, 0.0, 0.0]),
                        "bm25": models.SparseVector(indices=sparse.indices, values=sparse.values),
                    },
                    payload=p.model_dump(mode="json"),
                )
            ],
        )
    return client


def metas(payloads: list[KbPayload], **overrides: Any) -> dict[str, SnapshotMeta]:
    counts: dict[str, int] = {}
    for p in payloads:
        counts[p.snapshot_id] = counts.get(p.snapshot_id, 0) + 1
    recorded = {}
    for domain, snapshot_id in PINS.items():
        fields: dict[str, Any] = {
            "snapshot_id": snapshot_id,
            "collection": domain,
            "chunk_count": counts.get(snapshot_id, 0),
            "analyzer_version": bm25.ANALYZER_VERSION,
            "embed_model": EMBED_MODEL,
            "qdrant_collection": f"kb_{domain}",
        }
        recorded[snapshot_id] = SnapshotMeta(**(fields | overrides.get(domain, {})))
    return recorded


def config(
    tmp: Path,
    *,
    rerank: float = 0.5,
    bm25_threshold: float = 1.0,
    factor: float = 1.25,
    reranker: TeiModel = RERANK_MODEL,
    snapshots: Mapping[Collection, str] = PINS,
    limits: Mapping[str, int] | None = None,
) -> Path:
    """The real routing, lexicon and aliases, with thresholds and limits of the test's choosing."""
    for name in ("lexicon.yaml", "aliases.yaml"):
        shutil.copy(KB_CONFIG / name, tmp / name)
    routing = yaml.safe_load((KB_CONFIG / "routing.yaml").read_text(encoding="utf-8"))
    routing["limits"] |= limits or {}
    (tmp / "routing.yaml").write_text(yaml.safe_dump(routing, allow_unicode=True))
    thresholds = {
        "degraded_factor": factor,
        "reranker": {"model_id": reranker.model_id, "model_sha": reranker.model_sha},
        "snapshots": {s: {"rerank": rerank, "bm25": bm25_threshold} for s in snapshots.values()},
    }
    (tmp / "thresholds.yaml").write_text(yaml.safe_dump(thresholds))
    return tmp


async def service(
    tmp: Path,
    payloads: list[KbPayload],
    tei: FakeTei,
    *,
    recorded: dict[str, SnapshotMeta] | None = None,
    vectors: Mapping[str, list[float]] | None = None,
    **thresholds: Any,
) -> RetrievalService:
    known = metas(payloads) if recorded is None else recorded
    qdrant = await qdrant_with(payloads, vectors)
    return RetrievalService(tei, qdrant, known.get, config_dir=config(tmp, **thresholds))


def ctx(**overrides: Any) -> RetrievalContext:
    fields: dict[str, Any] = {
        "fsm_state": "S3",
        "language": "en",
        "as_of": AS_OF,
        "corpus_pins": PINS,
    }
    return RetrievalContext(**(fields | overrides))
