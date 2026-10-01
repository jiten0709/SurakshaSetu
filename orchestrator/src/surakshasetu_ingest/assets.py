"""Knowledge-base ingestion as Dagster assets (TDD §2.2, §7.5 step 3):

    source_docs -> parsed -> chunks -> enriched -> approved -> vectors -> indexed_snapshot

The review manifest names each collection's snapshot. A snapshot already recorded in
catalog.corpus_snapshot is immutable: the same chunks make its collection a no-op, other chunks
fail the run. Nothing reaches Qdrant or the catalog unless every document parsed, chunked and
embedded. `make kb-ingest` materialises the graph; `dagster dev -m surakshasetu_ingest.assets`
shows it.
"""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass
from io import BytesIO
from itertools import batched
from pathlib import Path
from statistics import fmean
from typing import Any

import psycopg
from dagster import ConfigurableResource, Definitions, Failure, asset
from docling.datamodel.base_models import DocumentStream, InputFormat
from docling.document_converter import DocumentConverter
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.group import GroupItem
from docling_core.types.doc.items.node import NodeItem
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.text import ListItem, SectionHeaderItem, TextItem, TitleItem
from docling_core.types.doc.labels import GroupLabel
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from qdrant_client import QdrantClient, models

from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.gateway import Gateway, TeiModel
from surakshasetu.kb.chunker import (
    SIZE_BOUNDS,
    Block,
    ChunkingError,
    RawChunk,
    chunk_document,
    count_tokens,
)
from surakshasetu.kb.manifest import (
    DocMeta,
    DocReview,
    Manifest,
    SnapshotRelease,
    enrich,
    load_manifest,
    read_source,
    review_gate,
)
from surakshasetu.kb.payload import KbPayload, indexed_text, point_id
from surakshasetu.retrieval import bm25

# Named into surakshasetu.kb so SS_LOG_DIR files it with the rest of the knowledge base (kb.log).
logger = logging.getLogger("surakshasetu.kb.ingest")

REPO = Path(__file__).resolve().parents[3]
EMBED_BATCH = 16
# ponytail: sized for CPU TEI on a laptop (a 16-chunk batch takes seconds to tens of seconds); a
# GPU needs a fraction. Queries keep the route's own budget.
EMBED_BATCH_TIMEOUT_S = 300.0

# TDD §7.2, plus review.status (Step 12's mandatory filter) and each collection's own filters.
PAYLOAD_INDEXES: list[tuple[str, models.PayloadSchemaType]] = [
    ("product_uin", models.PayloadSchemaType.KEYWORD),
    ("doc_type", models.PayloadSchemaType.KEYWORD),
    ("status", models.PayloadSchemaType.KEYWORD),
    ("snapshot_id", models.PayloadSchemaType.KEYWORD),
    ("review.status", models.PayloadSchemaType.KEYWORD),
    ("effective_from", models.PayloadSchemaType.DATETIME),
    ("effective_to", models.PayloadSchemaType.DATETIME),
]
EXTRA_INDEXES: dict[str, list[tuple[str, models.PayloadSchemaType]]] = {
    "regulatory": [("instrument", models.PayloadSchemaType.KEYWORD)],
    "product": [],
    "tax": [
        ("statute", models.PayloadSchemaType.KEYWORD),
        ("regime", models.PayloadSchemaType.KEYWORD),
        ("tax_years", models.PayloadSchemaType.KEYWORD),
    ],
}


class IngestSettings(Settings):
    # Only ingestion writes catalog.corpus_snapshot, as catalog_loader; the conversation tier
    # never holds this DSN. The dev default is compose's dummy password.
    pg_dsn_catalog_loader: SecretStr = SecretStr(
        "postgresql://catalog_loader:surakshasetu-dev-catalog-loader@127.0.0.1:5432/surakshasetu"
    )


class Kb(ConfigurableResource):  # type: ignore[type-arg]
    kb_dir: str = str(REPO / "content" / "seed" / "kb")
    uri_base: str = "content/seed/kb"  # source_uri = <uri_base>/<path>#<section_id>
    collection_prefix: str = ""  # tests index into collections of their own

    def collection(self, domain: str) -> str:
        return f"{self.collection_prefix}kb_{domain}"

    def manifest(self) -> Manifest:
        return load_manifest(Path(self.kb_dir) / "review-manifest.yaml")


@dataclass(frozen=True)
class Source:
    review: DocReview
    meta: DocMeta
    body: str  # Markdown, front matter removed


@dataclass(frozen=True)
class Sources:
    manifest: Manifest
    docs: list[Source]


@dataclass(frozen=True)
class Payloads:  # Dagster cannot type a list of an Annotated union, so it passes this instead
    items: list[KbPayload]


@dataclass(frozen=True)
class Point:
    payload: KbPayload
    dense: list[float]
    sparse: bm25.Sparse


@dataclass(frozen=True)
class Vectors:
    points: list[Point]
    avgdl: dict[str, float]
    embed_model: TeiModel
    embed_dim: int


@asset
def source_docs(kb: Kb) -> Sources:
    manifest = kb.manifest()
    docs = []
    for review in manifest.documents:
        try:
            meta, body = read_source(Path(kb.kb_dir) / review.path)
        except (OSError, ValueError) as exc:
            raise Failure(f"cannot read {review.doc_id} ({review.path})") from exc
        docs.append(Source(review, meta, body))
    logger.info("source docs: %d in the manifest", len(docs))
    return Sources(manifest, docs)


@asset
def parsed(source_docs: Sources) -> dict[str, list[Block]]:
    converter = DocumentConverter(allowed_formats=[InputFormat.MD])
    blocks: dict[str, list[Block]] = {}
    for doc in source_docs.docs:
        name = doc.review.doc_id
        try:
            stream = DocumentStream(
                name=Path(doc.review.path).name, stream=BytesIO(doc.body.encode())
            )
            blocks[name] = to_blocks(converter.convert(stream).document)
        except Exception as exc:
            raise Failure(f"Docling could not parse {name} ({doc.review.path})") from exc
        if not any(b.level is None for b in blocks[name]):
            raise Failure(f"Docling found no text in {name} ({doc.review.path})")
    return blocks


def to_blocks(document: DoclingDocument) -> list[Block]:
    """Docling's Markdown items as headings and paragraphs: a list or an inline-formatted run
    becomes one paragraph, a table its Markdown."""
    blocks = []
    for ref in document.body.children:
        item = ref.resolve(document)
        if isinstance(item, TitleItem):
            blocks.append(Block(1, item.text))
        elif isinstance(item, SectionHeaderItem):
            blocks.append(Block(item.level + 1, item.text))  # "##" is Docling's level 1
        elif isinstance(item, TableItem):
            blocks.append(Block(None, item.export_to_markdown(document)))
        elif isinstance(item, TextItem | GroupItem):
            blocks.append(Block(None, _text(item, document)))
    return [b for b in blocks if b.text.strip()]


def _text(item: NodeItem, document: DoclingDocument) -> str:
    if isinstance(item, TextItem):
        return f"- {item.text}" if isinstance(item, ListItem) else item.text
    parts = [_text(ref.resolve(document), document) for ref in item.children]
    joiner = " " if isinstance(item, GroupItem) and item.label == GroupLabel.INLINE else "\n"
    return joiner.join(p for p in parts if p)


@asset
def chunks(source_docs: Sources, parsed: dict[str, list[Block]]) -> dict[str, list[RawChunk]]:
    out = {}
    for doc in source_docs.docs:
        name = doc.review.doc_id
        try:
            out[name] = chunk_document(
                parsed[name], root=doc.meta.breadcrumb, bounds=SIZE_BOUNDS[doc.review.collection]
            )
        except ChunkingError as exc:
            raise Failure(f"cannot chunk {name}: {exc}") from exc
    small = sum(
        count_tokens(c.text) < SIZE_BOUNDS[d.review.collection][0]
        for d in source_docs.docs
        for c in out[d.review.doc_id]
    )
    logger.info(
        "chunks: %d from %d docs (%d below their size target)",
        sum(map(len, out.values())),
        len(out),
        small,
    )
    return out


@asset
def enriched(kb: Kb, source_docs: Sources, chunks: dict[str, list[RawChunk]]) -> Payloads:
    payloads: list[KbPayload] = []
    for doc in source_docs.docs:
        snapshot_id = source_docs.manifest.snapshots[doc.review.collection].snapshot_id
        try:
            payloads += [
                enrich(doc.review, doc.meta, c, snapshot_id=snapshot_id, uri_base=kb.uri_base)
                for c in chunks[doc.review.doc_id]
            ]
        except ValueError as exc:
            raise Failure(f"cannot build payloads for {doc.review.doc_id}") from exc
    dupes = [cid for cid, n in Counter(p.chunk_id for p in payloads).items() if n > 1]
    if dupes:
        raise Failure(f"duplicate chunk ids: {', '.join(dupes)}")
    return Payloads(payloads)


@asset
def approved(source_docs: Sources, enriched: Payloads) -> Payloads:
    kept = review_gate(enriched.items, source_docs.manifest)
    logger.info("review gate: %d of %d chunks approved", len(kept), len(enriched.items))
    return Payloads(kept)


@asset
def vectors(approved: Payloads) -> Vectors:
    """Dense from tei-embed, sparse from the shared BM25 analyzer. An unavailable embed route
    fails the run: there is no BM25-only indexing."""
    return asyncio.run(_vectorise(approved.items, IngestSettings()))


async def _vectorise(payloads: list[KbPayload], settings: IngestSettings) -> Vectors:
    texts = [indexed_text(p) for p in payloads]
    async with Gateway(settings) as gateway:
        served = await gateway.embed_model()
        dense: list[list[float]] = []
        for batch in batched(texts, EMBED_BATCH):
            dense += await gateway.embed(
                list(batch), kind="document", timeout_s=EMBED_BATCH_TIMEOUT_S
            )
            logger.debug("embedded %d of %d chunks", len(dense), len(texts))
    tokens = [bm25.tokenize(t) for t in texts]
    avgdl: dict[str, float] = {
        domain: fmean(len(t) for p, t in zip(payloads, tokens, strict=True) if p.domain == domain)
        for domain in {p.domain for p in payloads}
    }
    points = [
        Point(p, d, bm25.doc_vector(t, avgdl[p.domain]))
        for p, d, t in zip(payloads, dense, tokens, strict=True)
    ]
    logger.info("vectors: %d chunks embedded by %s", len(points), served.model_id)
    return Vectors(points, avgdl, served, settings.embed_dim)


@asset
def indexed_snapshot(kb: Kb, source_docs: Sources, vectors: Vectors) -> dict[str, Any]:
    settings = IngestSettings()
    client = QdrantClient(url=settings.qdrant_url)
    dsn = settings.pg_dsn_catalog_loader.get_secret_value()
    with psycopg.connect(dsn, autocommit=True) as conn:
        return {
            domain: _index(
                client,
                conn,
                kb.collection(domain),
                domain,
                release,
                [p for p in vectors.points if p.payload.domain == domain],
                vectors,
            )
            for domain, release in source_docs.manifest.snapshots.items()
        }


def _index(
    client: QdrantClient,
    conn: psycopg.Connection[Any],
    name: str,
    domain: str,
    release: SnapshotRelease,
    points: list[Point],
    vectors: Vectors,
) -> dict[str, Any]:
    snapshot_id = release.snapshot_id
    ids = sorted(p.payload.chunk_id for p in points)
    if not ids:
        raise Failure(f"{snapshot_id}: no approved chunks")
    chunks_sha256 = sha256_hex(ids)
    recorded = conn.execute(
        "SELECT collection, meta->>'chunks_sha256' FROM catalog.corpus_snapshot"
        " WHERE snapshot_id = %s",
        (snapshot_id,),
    ).fetchone()
    if recorded is not None:
        if recorded != (domain, chunks_sha256):
            raise Failure(
                f"{snapshot_id} is already recorded with other chunks: a released snapshot is"
                " immutable, so name a new snapshot_id in the review manifest"
            )
        logger.info("%s: %s unchanged (%d chunks), nothing to do", name, snapshot_id, len(ids))
        return {"snapshot_id": snapshot_id, "chunk_count": len(ids), "changed": False}

    _ensure_collection(client, name, domain, vectors.embed_dim)
    this_snapshot = models.Filter(
        must=[models.FieldCondition(key="snapshot_id", match=models.MatchValue(value=snapshot_id))]
    )
    # Not recorded yet, so any points under this id are a failed build's leftovers.
    client.delete(name, points_selector=models.FilterSelector(filter=this_snapshot), wait=True)
    for batch in batched(points, 64):
        client.upsert(name, points=[_point(snapshot_id, p) for p in batch], wait=True)
    count = client.count(name, count_filter=this_snapshot, exact=True).count
    if count != len(ids):
        raise Failure(f"{name}: {count} points under {snapshot_id}, expected {len(ids)}")
    snapshot = client.create_snapshot(name, wait=True)
    if snapshot is None:
        raise Failure(f"{name}: Qdrant took no snapshot")
    meta = {
        "avgdl": vectors.avgdl[domain],
        "analyzer_version": bm25.ANALYZER_VERSION,
        "embed_model": {
            "model_id": vectors.embed_model.model_id,
            "model_sha": vectors.embed_model.model_sha,
        },
        "embed_dim": vectors.embed_dim,
        "qdrant_collection": name,
        "qdrant_snapshot": snapshot.name,
        "chunks_sha256": chunks_sha256,
    }
    with conn.transaction():
        conn.execute(
            "UPDATE catalog.corpus_snapshot SET status = 'superseded'"
            " WHERE collection = %s AND status = 'active'",
            (domain,),
        )
        conn.execute(
            "INSERT INTO catalog.corpus_snapshot"
            " (snapshot_id, collection, chunk_count, built_at, approved_by, status, meta)"
            " VALUES (%s, %s, %s, now(), %s, 'active', %s)",
            (snapshot_id, domain, len(ids), release.approved_by, Jsonb(meta)),
        )
    logger.info("%s: %s indexed and active (%d chunks)", name, snapshot_id, len(ids))
    return {"snapshot_id": snapshot_id, "chunk_count": len(ids), "changed": True}


def _ensure_collection(client: QdrantClient, name: str, domain: str, dim: int) -> None:
    """TDD §7.2: one dense and one sparse vector, and the payload fields retrieval filters on."""
    if not client.collection_exists(name):
        client.create_collection(
            name,
            vectors_config={
                "dense": models.VectorParams(size=dim, distance=models.Distance.COSINE)
            },
            sparse_vectors_config={"bm25": models.SparseVectorParams(modifier=models.Modifier.IDF)},
        )
    for field, schema in PAYLOAD_INDEXES + EXTRA_INDEXES[domain]:
        client.create_payload_index(name, field_name=field, field_schema=schema, wait=True)


def _point(snapshot_id: str, point: Point) -> models.PointStruct:
    return models.PointStruct(
        id=str(point_id(snapshot_id, point.payload.chunk_id)),
        vector={
            "dense": point.dense,
            "bm25": models.SparseVector(indices=point.sparse.indices, values=point.sparse.values),
        },
        payload=point.payload.model_dump(mode="json"),
    )


ASSETS = [source_docs, parsed, chunks, enriched, approved, vectors, indexed_snapshot]
defs = Definitions(assets=ASSETS, resources={"kb": Kb()})
