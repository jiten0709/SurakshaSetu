"""The retrieval service (TDD §2.4-2.5, §1.5): a small, quota-balanced, citable evidence set from
the session's pinned corpus snapshots, or an abstention.

    rewrite -> route -> verify pins -> hybrid search per collection -> rerank -> select -> gate

- Search is the TDD §2.4 hybrid() query: dense top-40 and BM25 top-40 prefetch, RRF-fused, under
  the mandatory filters (the pinned snapshot, in force, effective on the as-of IST date, approved)
  and the contextual ones (UINs in focus, regime, tax year).
- A pinned snapshot that is not recorded, not the analyzer's, not the served embedding model's, or
  not fully in Qdrant is a corpus defect: RetrievalUnavailable, never a silent mismatch. So is a
  pin without calibrated thresholds, or a reranker other than the thresholds'.
- embed down: BM25-only search, still reranked. rerank down: BM25-only search scored by BM25. Both
  gate on a stricter threshold (degraded_factor) and mark the audit degraded.

Retrieval explains and cites; it never supplies a product number or disclosure text (TDD §2.1).
Deterministic for fixed inputs and snapshots. Never logs a query or chunk text.
"""

import asyncio
import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool
from pydantic import AwareDatetime, BaseModel, ConfigDict, ValidationError
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from surakshasetu.analysis.models import Intent
from surakshasetu.gateway import GatewayUnavailable, TeiModel
from surakshasetu.kb.payload import PAYLOAD, Collection, KbPayload, TaxYear, Uin, indexed_text
from surakshasetu.retrieval import bm25
from surakshasetu.retrieval.gate import Scored, Stamp, Thresholds, add_parents, insufficient, select
from surakshasetu.retrieval.rewrite import KB_CONFIG, Aliases, Lexicon, Rewrite, load, rewrite
from surakshasetu.retrieval.routing import RouteDecision, RoutingTable, route

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
Scoring = Literal["rerank", "bm25"]


class RetrievalUnavailable(Exception):
    """Retrieval cannot run on this session's pins; the caller answers from a template and offers
    an advisor."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Tei(Protocol):
    """The embed and rerank half of the gateway adapter (TEI, called directly)."""

    async def embed(
        self, texts: list[str], *, kind: Literal["query", "document"]
    ) -> list[list[float]]: ...
    async def embed_model(self) -> TeiModel: ...
    async def rerank(self, query: str, docs: list[str]) -> list[float]: ...
    async def rerank_model(self) -> TeiModel: ...


@dataclass(frozen=True)
class SnapshotMeta:
    """A catalog.corpus_snapshot row, as Step 11 recorded it. Immutable once recorded."""

    snapshot_id: str
    collection: Collection
    chunk_count: int
    analyzer_version: str
    embed_model: TeiModel
    qdrant_collection: str


class _RecordedMeta(BaseModel):
    analyzer_version: str
    embed_model: Stamp
    qdrant_collection: str


def load_snapshot_meta(
    pool: ConnectionPool[psycopg.Connection[TupleRow]], snapshot_id: str
) -> SnapshotMeta | None:
    """As app_rw, which has SELECT on catalog."""
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT collection, chunk_count, meta FROM catalog.corpus_snapshot"
            " WHERE snapshot_id = %s",
            (snapshot_id,),
        ).fetchone()
    if row is None:
        return None
    collection, chunk_count, meta = row
    recorded = _RecordedMeta.model_validate(meta)
    model = recorded.embed_model
    return SnapshotMeta(
        snapshot_id,
        collection,
        chunk_count,
        recorded.analyzer_version,
        TeiModel(model.model_id, model.model_sha),
        recorded.qdrant_collection,
    )


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RetrievalContext(_Frozen):
    fsm_state: str
    intents: list[Intent] = []
    entities: list[str] = []  # topic tags the caller already knows, e.g. "tax"
    focus_uins: list[Uin] = []  # products (and riders) the customer named or is shown
    regime: Literal["old", "new"] | None = None
    tax_year: TaxYear | None = None
    language: Literal["en", "hi", "hi-Latn"]
    as_of: AwareDatetime  # effective dates compare on its IST calendar date
    corpus_pins: dict[Collection, str]  # collection -> the session's pinned snapshot_id


class EvidenceChunk(_Frozen):
    handle: str  # E1..En, turn-local (TDD §2.5)
    chunk_id: str
    citation_label: str
    text: str
    domain: Collection
    rerank_score: float | None  # None in BM25-only scoring, or for a parent outside the pool
    content_sha256: str
    section_path: list[str]
    source_uri: str
    effective_from: date
    doc_title: str
    version: str
    doc_type: str
    precedence: int  # TDD §2.5 (1 highest): Step 14 cites the higher source when claims conflict
    parent: bool  # the parent section of a kept clause


class RetrievalAudit(_Frozen):
    """The RETRIEVAL event's payload (TDD §4.3): ids, hashes and scores, never chunk text. The
    rewritten queries are customer text, so they belong in the encrypted payload only."""

    rewritten_query: str
    lexical_query: str
    route_rule: str
    collections: list[Collection]
    snapshot_ids: list[str]
    candidates: dict[Collection, int]
    # The selection in evidence order, whether or not handles were issued.
    chunk_ids: list[str]
    content_sha256: list[str]
    rerank_scores: list[float | None]  # BM25 scores when scoring is bm25
    scoring: Scoring
    handle_map: dict[str, str]  # E# -> chunk_id; empty when abstained
    degraded: bool
    abstained: bool
    abstain_reason: str | None


class RetrievalResult(_Frozen):
    evidence: list[EvidenceChunk]  # empty when abstained
    abstained: bool
    abstain_reason: str | None
    audit: RetrievalAudit


@dataclass(frozen=True)
class Selection:
    """Everything before the sufficiency gate: what the offline metrics and calibration score."""

    rewrite: Rewrite
    decision: RouteDecision
    pins: dict[Collection, str]
    candidates: dict[Collection, int]
    evidence: list[Scored]  # kept, in rank order, then parents
    scoring: Scoring
    degraded: bool
    reranker: TeiModel | None  # what tei-rerank served, when scoring is rerank


def search_filter(domain: Collection, snapshot_id: str, ctx: RetrievalContext) -> models.Filter:
    """TDD §2.4's mandatory filters, then the contextual ones for this collection."""
    day = ctx.as_of.astimezone(IST).date()
    # Payload dates are indexed as DATETIME at 00:00Z, so the IST day compares at 00:00Z too.
    at = datetime(day.year, day.month, day.day, tzinfo=UTC)
    must: list[models.Condition] = [
        _equals("snapshot_id", snapshot_id),
        _equals("status", "in_force"),
        _equals("review.status", "approved"),
        models.FieldCondition(key="effective_from", range=models.DatetimeRange(lte=at)),
        models.Filter(
            should=[
                models.IsNullCondition(is_null=models.PayloadField(key="effective_to")),
                models.FieldCondition(key="effective_to", range=models.DatetimeRange(gte=at)),
            ]
        ),
    ]
    if domain == "product" and ctx.focus_uins:
        must.append(
            models.FieldCondition(key="product_uin", match=models.MatchAny(any=ctx.focus_uins))
        )
    if domain == "tax" and ctx.regime is not None:
        must.append(
            models.FieldCondition(key="regime", match=models.MatchAny(any=[ctx.regime, "both"]))
        )
    if domain == "tax" and ctx.tax_year is not None:
        must.append(_equals("tax_years", ctx.tax_year))
    return models.Filter(must=must)


class RetrievalService:
    def __init__(
        self,
        tei: Tei,
        qdrant: AsyncQdrantClient,
        snapshot_meta: Callable[[str], SnapshotMeta | None],
        *,
        config_dir: Path = KB_CONFIG,
    ) -> None:
        self._tei = tei
        self._qdrant = qdrant
        self._snapshot_meta = snapshot_meta
        self.routing = load(RoutingTable, config_dir / "routing.yaml")
        self._lexicon = load(Lexicon, config_dir / "lexicon.yaml")
        self._aliases = load(Aliases, config_dir / "aliases.yaml")
        thresholds = config_dir / "thresholds.yaml"
        # Absent until `make calibrate-retrieval`: then only select() runs, and decide() refuses.
        self.thresholds = load(Thresholds, thresholds) if thresholds.exists() else None
        self._verified: dict[str, SnapshotMeta] = {}  # immutable rows, so verified once

    async def retrieve(self, query: str, ctx: RetrievalContext) -> RetrievalResult:
        return self.decide(await self.select(query, ctx))

    async def select(self, query: str, ctx: RetrievalContext) -> Selection:
        written = rewrite(query, ctx.focus_uins, self._lexicon, self._aliases)
        decision = route(
            self.routing,
            fsm_state=ctx.fsm_state,
            intents=ctx.intents,
            entities=ctx.entities,
            tokens=written.tokens,
            product_in_focus=bool(ctx.focus_uins),
        )
        if decision.abstain_reason is not None:
            return Selection(written, decision, {}, {}, [], "rerank", False, None)
        if any(c not in ctx.corpus_pins for c in decision.collections):
            logger.error("retrieval: route %s needs a collection with no pin", decision.rule_id)
            raise RetrievalUnavailable("PIN_MISSING")
        pins = {c: ctx.corpus_pins[c] for c in decision.collections}
        verified = await asyncio.gather(*(self._verified_meta(c, s) for c, s in pins.items()))
        metas = dict(zip(pins, verified, strict=True))
        dense, reranker = await self._embed(written.semantic, metas)
        filters = {c: search_filter(c, pins[c], ctx) for c in pins}
        sparse = bm25.query_vector(written.tokens)
        pool = await self._search(metas, filters, dense, sparse)
        scores = await self._rerank(written.lexical, pool) if reranker is not None else None
        scoring: Scoring = "rerank"
        if scores is None:
            scoring = "bm25"
            if dense is not None:  # the fused scores mean nothing alone: search BM25 only
                pool = await self._search(metas, filters, None, sparse)
            ranked = [Scored(p, s) for p, s in pool]
        else:
            ranked = [Scored(p, s) for (p, _), s in zip(pool, scores, strict=True)]
        degraded = dense is None or scoring == "bm25"
        limits = self.routing.limits
        budget = limits.evidence_budget_tokens
        kept = select(ranked, decision.quotas, keep=limits.keep, budget=budget)
        documents = await self._documents(kept, metas, filters)
        scored = {s.payload.chunk_id: s.score for s in ranked if s.score is not None}
        candidates = {c: sum(p.domain == c for p, _ in pool) for c in pins}
        logger.debug("retrieval candidates %s, kept %d", candidates, len(kept))
        return Selection(
            rewrite=written,
            decision=decision,
            pins=pins,
            candidates=candidates,
            evidence=add_parents(kept, documents, scored, budget=budget),
            scoring=scoring,
            degraded=degraded,
            reranker=reranker if scoring == "rerank" else None,
        )

    def decide(self, selection: Selection, thresholds: Thresholds | None = None) -> RetrievalResult:
        """The sufficiency gate (TDD §2.4 step 5), then handles. Pure."""
        chosen = thresholds or self.thresholds
        reason = selection.decision.abstain_reason
        if reason is None:
            if chosen is None or any(s not in chosen.snapshots for s in selection.pins.values()):
                logger.error("retrieval: a pinned snapshot has no calibrated thresholds")
                raise RetrievalUnavailable("THRESHOLDS_MISSING")
            if selection.scoring == "rerank" and _stamp(selection.reranker) != chosen.reranker:
                logger.error("retrieval: the served reranker is not the thresholds' reranker")
                raise RetrievalUnavailable("RERANKER_MISMATCH")
            factor = chosen.degraded_factor if selection.degraded else 1.0
            snapshots, scoring = chosen.snapshots, selection.scoring

            def threshold(payload: KbPayload) -> float:
                pinned = snapshots[payload.snapshot_id]
                return (pinned.rerank if scoring == "rerank" else pinned.bm25) * factor

            reason = insufficient(selection.evidence, selection.decision.quotas, threshold)
        abstained = reason is not None
        handles = [f"E{i}" for i in range(1, len(selection.evidence) + 1)]
        evidence = (
            []
            if abstained
            else [
                self._chunk(h, s, selection.scoring)
                for h, s in zip(handles, selection.evidence, strict=True)
            ]
        )
        audit = RetrievalAudit(
            rewritten_query=selection.rewrite.semantic,
            lexical_query=selection.rewrite.lexical,
            route_rule=selection.decision.rule_id,
            collections=list(selection.pins),
            snapshot_ids=list(selection.pins.values()),
            candidates=selection.candidates,
            chunk_ids=[s.payload.chunk_id for s in selection.evidence],
            content_sha256=[s.payload.content_sha256 for s in selection.evidence],
            rerank_scores=[s.score for s in selection.evidence],
            scoring=selection.scoring,
            handle_map={e.handle: e.chunk_id for e in evidence},
            degraded=selection.degraded,
            abstained=abstained,
            abstain_reason=reason,
        )
        if abstained:
            logger.warning("retrieval abstained: %s (route %s)", reason, selection.decision.rule_id)
        else:
            logger.info(
                "retrieval route %s: %d evidence chunks from %s%s",
                selection.decision.rule_id,
                len(evidence),
                ",".join(selection.pins),
                " (degraded)" if selection.degraded else "",
            )
        return RetrievalResult(
            evidence=evidence, abstained=abstained, abstain_reason=reason, audit=audit
        )

    def _chunk(self, handle: str, item: Scored, scoring: Scoring) -> EvidenceChunk:
        p = item.payload
        return EvidenceChunk(
            handle=handle,
            chunk_id=p.chunk_id,
            citation_label=p.citation_label,
            text=p.text,
            domain=p.domain,
            rerank_score=item.score if scoring == "rerank" else None,
            content_sha256=p.content_sha256,
            section_path=p.section_path,
            source_uri=p.source_uri,
            effective_from=p.effective_from,
            doc_title=p.doc_title,
            version=p.version,
            doc_type=p.doc_type,
            precedence=self.routing.rank(p.doc_type),
            parent=item.parent,
        )

    async def _verified_meta(self, collection: Collection, snapshot_id: str) -> SnapshotMeta:
        meta = self._verified.get(snapshot_id)
        if meta is not None and meta.collection == collection:
            return meta
        meta = await asyncio.to_thread(self._snapshot_meta, snapshot_id)
        if meta is None or meta.collection != collection:
            raise _defect(
                "SNAPSHOT_UNKNOWN", "pinned %s snapshot %s is not recorded", collection, snapshot_id
            )
        if meta.analyzer_version != bm25.ANALYZER_VERSION:
            raise _defect(
                "ANALYZER_MISMATCH",
                "snapshot %s was indexed by analyzer %s",
                snapshot_id,
                meta.analyzer_version,
            )
        try:
            counted = await self._qdrant.count(
                meta.qdrant_collection,
                count_filter=models.Filter(must=[_equals("snapshot_id", snapshot_id)]),
                exact=True,
            )
            count = counted.count
        except UnexpectedResponse as exc:
            if exc.status_code != 404:  # 404: the collection itself is gone
                raise _qdrant_unavailable(exc) from exc
            count = 0
        except ResponseHandlingException as exc:
            raise _qdrant_unavailable(exc) from exc
        if count != meta.chunk_count:
            raise _defect(
                "SNAPSHOT_MISSING",
                "snapshot %s has %d of its %d points in Qdrant",
                snapshot_id,
                count,
                meta.chunk_count,
            )
        self._verified[snapshot_id] = meta
        return meta

    async def _embed(
        self, query: str, metas: dict[Collection, SnapshotMeta]
    ) -> tuple[list[float] | None, TeiModel | None]:
        """The query vector (None: embed is down) and the served reranker (None: rerank is down).
        The model checks run beside the embedding, so they add no wall time."""
        embedded, embed_model, rerank_model = await asyncio.gather(
            self._tei.embed([query], kind="query"),
            self._tei.embed_model(),
            self._tei.rerank_model(),
            return_exceptions=True,
        )
        for outcome in (embedded, embed_model, rerank_model):
            if isinstance(outcome, BaseException) and not isinstance(outcome, GatewayUnavailable):
                raise outcome
        reranker = rerank_model if isinstance(rerank_model, TeiModel) else None
        if isinstance(embedded, BaseException) or isinstance(embed_model, BaseException):
            logger.warning("retrieval degraded: embed unavailable, BM25-only search")
            return None, reranker
        stale = [m.snapshot_id for m in metas.values() if m.embed_model != embed_model]
        if stale:
            raise _defect(
                "EMBED_MODEL_MISMATCH",
                "snapshots %s were embedded by another model than tei-embed serves",
                ",".join(stale),
            )
        return embedded[0], reranker

    async def _search(
        self,
        metas: dict[Collection, SnapshotMeta],
        filters: dict[Collection, models.Filter],
        dense: list[float] | None,
        sparse: bm25.Sparse,
    ) -> list[tuple[KbPayload, float]]:
        """Every collection's candidates, collections in route order, each in Qdrant's order."""
        limit = self.routing.limits.per_collection
        lexical = models.SparseVector(indices=sparse.indices, values=sparse.values)

        async def one(domain: Collection) -> list[tuple[KbPayload, float]]:
            flt = filters[domain]
            prefetch = []
            if dense is not None:
                prefetch.append(
                    models.Prefetch(query=dense, using="dense", filter=flt, limit=limit)
                )
            if sparse.indices:
                prefetch.append(
                    models.Prefetch(query=lexical, using="bm25", filter=flt, limit=limit)
                )
            if not prefetch:
                return []
            with _qdrant_errors():
                if dense is None:  # BM25 only: its own scores, no fusion
                    response = await self._qdrant.query_points(
                        metas[domain].qdrant_collection,
                        query=lexical,
                        using="bm25",
                        query_filter=flt,
                        limit=limit,
                        with_payload=True,
                    )
                else:  # TDD §2.4 hybrid()
                    response = await self._qdrant.query_points(
                        metas[domain].qdrant_collection,
                        prefetch=prefetch,
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                        limit=limit,
                        with_payload=True,
                    )
            return [(_payload(p.payload), p.score) for p in response.points]

        found = await asyncio.gather(*(one(domain) for domain in metas))
        return [hit for hits in found for hit in hits]

    async def _rerank(self, query: str, pool: list[tuple[KbPayload, float]]) -> list[float] | None:
        try:
            return await self._tei.rerank(query, [indexed_text(p) for p, _ in pool])
        except GatewayUnavailable:
            logger.warning("retrieval degraded: rerank unavailable, BM25-only scoring")
            return None

    async def _documents(
        self,
        kept: list[Scored],
        metas: dict[Collection, SnapshotMeta],
        filters: dict[Collection, models.Filter],
    ) -> list[KbPayload]:
        """Every chunk of the kept chunks' documents (under the same filters), to find parents."""
        docs: dict[Collection, set[str]] = {}
        for item in kept:
            docs.setdefault(item.payload.domain, set()).add(item.payload.doc_id)

        async def one(domain: Collection, doc_ids: set[str]) -> list[KbPayload]:
            base = filters[domain].must
            flt = models.Filter(
                must=[
                    *(base if isinstance(base, list) else []),
                    models.FieldCondition(key="doc_id", match=models.MatchAny(any=sorted(doc_ids))),
                ]
            )
            records: list[models.Record] = []
            offset: Any = None
            with _qdrant_errors():
                while True:
                    batch, offset = await self._qdrant.scroll(
                        metas[domain].qdrant_collection,
                        scroll_filter=flt,
                        limit=256,
                        offset=offset,
                        with_payload=True,
                    )
                    records += batch
                    if offset is None:
                        break
            return [_payload(r.payload) for r in records]

        found = await asyncio.gather(*(one(d, ids) for d, ids in docs.items()))
        return [p for payloads in found for p in payloads]


def _equals(key: str, value: str) -> models.FieldCondition:
    return models.FieldCondition(key=key, match=models.MatchValue(value=value))


def _stamp(model: TeiModel | None) -> Stamp | None:
    return None if model is None else Stamp(model_id=model.model_id, model_sha=model.model_sha)


def _payload(raw: dict[str, Any] | None) -> KbPayload:
    try:
        return PAYLOAD.validate_python(raw)
    except ValidationError as exc:
        raise _defect("CORPUS_INVALID", "a point fails the payload schema") from exc


def _defect(reason: str, message: str, *args: object) -> RetrievalUnavailable:
    """A corpus defect: logged for compliance, and retrieval refuses rather than guess."""
    logger.error("corpus defect: " + message, *args)
    return RetrievalUnavailable(reason)


@contextmanager
def _qdrant_errors() -> Iterator[None]:
    try:
        yield
    except (UnexpectedResponse, ResponseHandlingException) as exc:
        raise _qdrant_unavailable(exc) from exc


def _qdrant_unavailable(exc: Exception) -> RetrievalUnavailable:
    # The exception text can carry the endpoint: name its type only.
    logger.warning("retrieval: qdrant call failed (%s)", type(exc).__name__)
    return RetrievalUnavailable("QDRANT_UNAVAILABLE")
