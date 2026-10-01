"""make kb-verify: the indexed knowledge base agrees with the review manifest, the payload schema,
the DUMMY marker and the golden sets."""

import logging
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
import yaml
from pydantic import ValidationError
from qdrant_client import QdrantClient, models

from surakshasetu.kb.golden import GoldenSet, UnanswerableSet
from surakshasetu.kb.payload import COLLECTIONS, PAYLOAD, point_id
from surakshasetu_ingest.assets import REPO, IngestSettings, Kb

logger = logging.getLogger("surakshasetu.kb.verify")

GOLDEN = REPO / "content" / "golden"
GOLDEN_PER_COLLECTION = 20
UNANSWERABLE = 10


def verify(kb: Kb, settings: IngestSettings, golden: Path = GOLDEN) -> list[str]:
    """Every problem found, one line each; empty means the knowledge base is sound."""
    manifest = kb.manifest()
    not_approved = {d.doc_id for d in manifest.documents if d.status != "approved"}
    client = QdrantClient(url=settings.qdrant_url)
    with psycopg.connect(settings.pg_dsn_catalog_loader.get_secret_value()) as conn:
        active = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT collection, snapshot_id, chunk_count FROM catalog.corpus_snapshot"
                " WHERE status = 'active'"
            )
        }
    problems: list[str] = []
    for domain in COLLECTIONS:
        if domain not in active:
            problems.append(f"{domain}: no active snapshot")
            continue
        snapshot_id, chunk_count = active[domain]
        release = manifest.snapshots.get(domain)
        if release is None or release.snapshot_id != snapshot_id:
            problems.append(f"{domain}: active snapshot {snapshot_id} is not the manifest's")
        points = _points(client, kb.collection(domain), snapshot_id)
        ids = set()
        docs: Counter[str] = Counter()
        for point in points:
            try:
                payload = PAYLOAD.validate_python(point.payload)
            except ValidationError as exc:
                problems.append(f"{domain}: point {point.id} fails the payload schema ({exc})")
                continue
            ids.add(payload.chunk_id)
            docs[payload.doc_id] += 1
            if not payload.text.startswith("DUMMY"):
                problems.append(f"{payload.chunk_id}: text does not start with DUMMY")
            if payload.doc_id in not_approved or payload.review.status != "approved":
                problems.append(f"{payload.chunk_id}: from a document that is not approved")
            if str(point.id) != str(point_id(snapshot_id, payload.chunk_id)):
                problems.append(f"{payload.chunk_id}: point id is not UUIDv5(snapshot/chunk)")
        if len(points) != chunk_count:
            problems.append(f"{domain}: {len(points)} points, snapshot row says {chunk_count}")
        logger.info("%-10s %s: %d points from %d docs", domain, snapshot_id, len(points), len(docs))
        problems += _golden(golden / "retrieval" / f"{domain}.yaml", domain, snapshot_id, ids)
    unanswerable = UnanswerableSet.model_validate(_yaml(golden / "unanswerable.yaml"))
    if len(unanswerable.questions) != UNANSWERABLE:
        problems.append(
            f"unanswerable: {len(unanswerable.questions)} questions, not {UNANSWERABLE}"
        )
    for problem in problems:
        logger.error("kb-verify: %s", problem)
    logger.info("kb-verify: %d problem(s)", len(problems))
    return problems


def _points(client: QdrantClient, name: str, snapshot_id: str) -> list[models.Record]:
    this_snapshot = models.Filter(
        must=[models.FieldCondition(key="snapshot_id", match=models.MatchValue(value=snapshot_id))]
    )
    points: list[models.Record] = []
    offset = None
    while True:
        batch, offset = client.scroll(
            name, scroll_filter=this_snapshot, limit=256, offset=offset, with_payload=True
        )
        points += batch
        if offset is None:
            return points


def _golden(path: Path, domain: str, snapshot_id: str, ids: set[str]) -> list[str]:
    golden = GoldenSet.model_validate(_yaml(path))
    problems = []
    if golden.collection != domain or golden.snapshot_id != snapshot_id:
        problems.append(f"{path.name}: labelled on {golden.snapshot_id}, active is {snapshot_id}")
    if len(golden.questions) != GOLDEN_PER_COLLECTION:
        problems.append(f"{path.name}: {len(golden.questions)} questions")
    problems += [
        f"{path.name} {q.id}: gold chunk {g} is not in {snapshot_id}"
        for q in golden.questions
        for g in q.gold_chunk_ids
        if g not in ids
    ]
    languages = Counter(q.language for q in golden.questions)
    logger.info("%-10s golden: %s", domain, dict(sorted(languages.items())))
    return problems


def _yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))
