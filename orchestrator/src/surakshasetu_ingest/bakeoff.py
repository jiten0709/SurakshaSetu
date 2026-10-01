"""The embedder bake-off (Step 12, guide 7b; the human makes the call): `make bakeoff-embed`.

The challenger embedder (compose profile bakeoff, :8083) indexes the approved chunks through the
Step 11 assets into bakeoff_kb_<domain>: the same chunks, BM25 vectors and snapshot ids, and no
catalog row, so the dev snapshots are untouched. Then both embedders answer the golden questions
with the same first stage (the rewritten query, dense top-40 plus BM25 top-40, RRF, the mandatory
filters) and each gold chunk's fused rank is scored: recall@8, @16 and @40 and MRR, per collection
and language. At seed scale recall@40 is degenerate (40 of about 45 points per collection), so the
lower cut-offs separate the models. Everything goes through the adapter and TEI; no remote code.
"""

import asyncio
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime
from statistics import fmean

from dagster import materialize
from qdrant_client import AsyncQdrantClient, QdrantClient, models

from surakshasetu.analysis.models import Intent
from surakshasetu.gateway import Gateway
from surakshasetu.kb.golden import load_golden
from surakshasetu.kb.payload import COLLECTIONS, Collection, KbPayload
from surakshasetu.retrieval import bm25
from surakshasetu.retrieval.rewrite import KB_CONFIG, Aliases, Lexicon, load, rewrite
from surakshasetu.retrieval.service import RetrievalContext, search_filter
from surakshasetu_ingest import assets

logger = logging.getLogger("surakshasetu.kb.bakeoff")

PREFIX = "bakeoff_"
CUTOFFS = (8, 16, 40)
# The challenger is instruction-tuned: queries (never documents) carry a task instruction.
QUERY_INSTRUCTION = (
    "Instruct: Given a customer's question about Indian life insurance, retrieve the policy,"
    " regulatory or tax passages that answer it\nQuery: "
)
GOLDEN = KB_CONFIG.parent / "golden" / "retrieval"


def bakeoff_embed(settings: assets.IngestSettings) -> int:
    kb = assets.Kb(collection_prefix=PREFIX)
    result = materialize(assets.ASSETS[:5], resources={"kb": kb})  # through the review gate
    approved: list[KbPayload] = result.output_for_node("approved").items
    challenger = settings.model_copy(
        update={
            "tei_embed_url": os.environ.get("SS_BAKEOFF_EMBED_URL", "http://127.0.0.1:8083"),
            "embed_query_prefix": os.environ.get("SS_BAKEOFF_QUERY_PREFIX", QUERY_INSTRUCTION),
        }
    )
    vectors = asyncio.run(assets._vectorise(approved, challenger))
    manifest = kb.manifest()
    client = QdrantClient(url=settings.qdrant_url)
    for domain in COLLECTIONS:
        name = kb.collection(domain)
        snapshot_id = manifest.snapshots[domain].snapshot_id
        assets._ensure_collection(client, name, domain, vectors.embed_dim)
        pinned = models.Filter(
            must=[
                models.FieldCondition(key="snapshot_id", match=models.MatchValue(value=snapshot_id))
            ]
        )
        client.delete(name, points_selector=models.FilterSelector(filter=pinned), wait=True)
        points = [
            assets._point(snapshot_id, p) for p in vectors.points if p.payload.domain == domain
        ]
        client.upsert(name, points=points, wait=True)
        logger.info("%s: %d points under %s", name, len(points), snapshot_id)
    asyncio.run(_compare(settings, challenger))
    return 0


async def _compare(baseline: assets.IngestSettings, challenger: assets.IngestSettings) -> None:
    lexicon = load(Lexicon, KB_CONFIG / "lexicon.yaml")
    aliases = load(Aliases, KB_CONFIG / "aliases.yaml")
    qdrant = AsyncQdrantClient(url=baseline.qdrant_url)
    ranks: dict[tuple[str, Collection, str], list[list[int]]] = defaultdict(list)
    models_served = {}
    for name, settings, prefix in (("baseline", baseline, ""), ("challenger", challenger, PREFIX)):
        async with Gateway(settings) as gateway:
            served = await gateway.embed_model()
            models_served[name] = f"{served.model_id}@{served.model_sha[:8]}"
            for domain in COLLECTIONS:
                golden = load_golden(GOLDEN / f"{domain}.yaml")
                ctx = RetrievalContext(
                    fsm_state="S3",
                    intents=[Intent.SIDE_QUERY],
                    language="en",
                    as_of=datetime.now(UTC),
                    corpus_pins={domain: golden.snapshot_id},
                )
                flt = search_filter(domain, golden.snapshot_id, ctx)
                for q in golden.questions:
                    written = rewrite(q.question, [], lexicon, aliases)
                    dense = (await gateway.embed([written.semantic], kind="query"))[0]
                    sparse = bm25.query_vector(written.tokens)
                    lexical = models.SparseVector(indices=sparse.indices, values=sparse.values)
                    response = await qdrant.query_points(
                        f"{prefix}kb_{domain}",
                        prefetch=[
                            models.Prefetch(query=dense, using="dense", filter=flt, limit=40),
                            models.Prefetch(query=lexical, using="bm25", filter=flt, limit=40),
                        ],
                        query=models.FusionQuery(fusion=models.Fusion.RRF),
                        limit=40,
                        with_payload=["chunk_id"],
                    )
                    found = [(p.payload or {})["chunk_id"] for p in response.points]
                    gold = [found.index(g) + 1 if g in found else 0 for g in q.gold_chunk_ids]
                    for language in ("all", q.language):
                        ranks[(name, domain, language)].append(gold)
    logger.info(
        "baseline %s; challenger %s", models_served["baseline"], models_served["challenger"]
    )
    logger.info(
        "%-11s %-8s %-11s %3s %7s %7s %7s %6s", "collection", "language", "embedder", "n",
        "r@8", "r@16", "r@40", "MRR",
    )  # fmt: skip
    for domain in COLLECTIONS:
        for language in ("all", "en", "hi", "hi-Latn"):
            for name in ("baseline", "challenger"):
                rows = ranks.get((name, domain, language), [])
                if not rows:
                    continue
                recall = [fmean(fmean(0 < r <= k for r in gold) for gold in rows) for k in CUTOFFS]
                mrr = fmean(1 / min(r for r in gold if r) if any(gold) else 0.0 for gold in rows)
                logger.info(
                    "%-11s %-8s %-11s %3d %7.3f %7.3f %7.3f %6.3f",
                    domain, language, name, len(rows), *recall, mrr,
                )  # fmt: skip
