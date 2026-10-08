"""Offline retrieval metrics (TDD §5.2) and sufficiency-gate calibration, on the golden sets:

    python -m surakshasetu.eval.retrieval_metrics calibrate|eval|bakeoff-rerank

- context recall@8: per question, the gold chunks among the first 8 evidence chunks, over the gold
  chunks; averaged per collection (RAGAS scores per sample).
- context precision@8: rank-aware, as RAGAS: the sum of precision@k at each relevant rank k, over
  the relevant chunks in the top 8. The gold lists hold 1-6 chunks, so dividing by the chunks
  returned (§5.2's wording) could never reach 0.80.
- abstention accuracy: answerable questions answered plus unanswerable questions abstained, over
  all of them. §5.2's unanswerable-only figure is printed too.

Recall and precision score the selection before the gate; the gate is scored by abstention.
calibrate runs the pipeline once per question (and once more BM25-only, for the rerank-down
thresholds), picks per-snapshot thresholds and writes content/kb/thresholds.yaml. eval reads that
file and exits 1 below the gates. Needs the stack: Qdrant, TEI and the dev catalog as app_rw.
"""

import argparse
import asyncio
import json
import logging
import math
import os
import time
from collections import defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal

import yaml
from psycopg_pool import ConnectionPool
from qdrant_client import AsyncQdrantClient, models

from surakshasetu.analysis.models import Intent
from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.gateway import Gateway, GatewayUnavailable, TeiModel
from surakshasetu.kb.golden import GoldenSet, Language, load_golden, load_unanswerable
from surakshasetu.kb.payload import COLLECTIONS, PAYLOAD, Collection, indexed_text
from surakshasetu.logging import configure_logging
from surakshasetu.retrieval.gate import SnapshotThresholds, Stamp, Thresholds
from surakshasetu.retrieval.rewrite import KB_CONFIG
from surakshasetu.retrieval.service import (
    RetrievalContext,
    RetrievalService,
    Selection,
    SnapshotMeta,
    load_snapshot_meta,
)

logger = logging.getLogger("surakshasetu.eval.retrieval")

K = 8
GATES = {"recall": 0.90, "precision": 0.80, "abstention": 0.95}
DEGRADED_FACTOR = 1.25  # TDD §1.5's "stricter threshold" for BM25-only retrieval
REPO = KB_CONFIG.parents[1]
GOLDEN = REPO / "content" / "golden"
RERANK_CACHE = REPO / "orchestrator" / ".cache" / "rerank"  # gitignored


# --- metrics (pure) ---------------------------------------------------------------------------


def recall_at_k(gold: Iterable[str], ranked: Sequence[str], k: int = K) -> float:
    wanted = set(gold)
    return len(wanted & set(ranked[:k])) / len(wanted)


def context_precision_at_k(gold: Iterable[str], ranked: Sequence[str], k: int = K) -> float:
    wanted = set(gold)
    hits, total = 0, 0.0
    for rank, chunk in enumerate(ranked[:k], start=1):
        if chunk in wanted:
            hits += 1
            total += hits / rank
    return total / hits if hits else 0.0


def calibrate(positives: Sequence[float], negatives: Sequence[float]) -> float:
    """The threshold that best splits answerable best-scores (at or above) from unanswerable ones
    (below): of the cuts with the highest accuracy, the one in the widest gap, then the lowest.
    Cuts are the midpoints between neighbouring scores, and one below them all."""
    values = sorted(set(positives) | set(negatives))
    if not values:
        raise ValueError("nothing to calibrate on")
    cuts = [(values[0] / 2, values[0])] + [
        ((low + high) / 2, high - low) for low, high in zip(values, values[1:], strict=False)
    ]

    def accuracy(cut: float) -> int:
        return sum(p >= cut for p in positives) + sum(n < cut for n in negatives)

    best = max(cuts, key=lambda c: (accuracy(c[0]), c[1], -c[0]))[0]
    return _round_within(best, values)


def _round_within(cut: float, values: list[float]) -> float:
    """Six decimals, unless that would move the cut across a score."""
    rounded = round(cut, 6)
    below = sum(v < cut for v in values) == sum(v < rounded for v in values)
    return rounded if below else cut


# --- the TEI wrappers the evaluation runs through ----------------------------------------------


class CachedRerank:
    """The gateway, with rerank scores cached on disk per (reranker, query, document), so a
    re-run on CPU only reranks pairs it has not seen. Evaluation only: the service never caches."""

    def __init__(self, gateway: Gateway, cache_dir: Path = RERANK_CACHE) -> None:
        self._gateway = gateway
        self._dir = cache_dir
        self._model: TeiModel | None = None
        self._scores: dict[str, float] = {}

    async def embed(
        self, texts: list[str], *, kind: Literal["query", "document"]
    ) -> list[list[float]]:
        return await self._gateway.embed(texts, kind=kind)

    async def embed_model(self) -> TeiModel:
        return await self._gateway.embed_model()

    async def rerank_model(self) -> TeiModel:
        if self._model is None:
            self._model = await self._gateway.rerank_model()
            if self._file.exists():
                self._scores = json.loads(self._file.read_text(encoding="utf-8"))
        return self._model

    async def rerank(self, query: str, docs: list[str]) -> list[float]:
        model = await self.rerank_model()
        keys = [sha256_hex([model.model_id, model.model_sha, query, doc]) for doc in docs]
        missing = [i for i, key in enumerate(keys) if key not in self._scores]
        if missing:
            fresh = await self._gateway.rerank(query, [docs[i] for i in missing])
            self._scores.update(zip([keys[i] for i in missing], fresh, strict=True))
            self._dir.mkdir(parents=True, exist_ok=True)
            partial_file = self._file.with_suffix(".tmp")
            partial_file.write_text(json.dumps(self._scores), encoding="utf-8")
            partial_file.replace(self._file)
        return [self._scores[key] for key in keys]

    @property
    def _file(self) -> Path:
        model = self._model or TeiModel("", "")  # set by rerank_model() before any use
        return self._dir / f"{sha256_hex([model.model_id, model.model_sha])}.json"


class Bm25Only:
    """Embed and rerank both down: the service's BM25-only path, for the rerank-down thresholds."""

    async def embed(
        self, texts: list[str], *, kind: Literal["query", "document"]
    ) -> list[list[float]]:
        raise GatewayUnavailable("UNAVAILABLE")

    async def embed_model(self) -> TeiModel:
        raise GatewayUnavailable("UNAVAILABLE")

    async def rerank(self, query: str, docs: list[str]) -> list[float]:
        raise GatewayUnavailable("UNAVAILABLE")

    async def rerank_model(self) -> TeiModel:
        raise GatewayUnavailable("UNAVAILABLE")


# --- the golden run -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Question:
    id: str
    text: str
    language: Language
    collection: Collection | None  # None: unanswerable
    gold: tuple[str, ...]


@dataclass(frozen=True)
class Outcome:
    question: Question
    selection: Selection

    @property
    def ranked(self) -> list[str]:
        return [s.payload.chunk_id for s in self.selection.evidence]

    def best(self, domain: Collection) -> float | None:
        scores = [
            s.score
            for s in self.selection.evidence
            if s.payload.domain == domain and not s.parent and s.score is not None
        ]
        return max(scores, default=None)


def questions(golden: Path = GOLDEN) -> tuple[list[Question], dict[Collection, str]]:
    sets: list[GoldenSet] = [load_golden(golden / "retrieval" / f"{c}.yaml") for c in COLLECTIONS]
    answerable = [
        Question(q.id, q.question, q.language, s.collection, tuple(q.gold_chunk_ids))
        for s in sets
        for q in s.questions
    ]
    unanswerable = [
        Question(q.id, q.question, q.language, None, ())
        for q in load_unanswerable(golden / "unanswerable.yaml").questions
    ]
    return answerable + unanswerable, {s.collection: s.snapshot_id for s in sets}


def eval_context(question: Question, pins: dict[Collection, str]) -> RetrievalContext:
    """A side-query in S3 (so the I3 gate is open), with no product, regime or year in focus."""
    return RetrievalContext(
        fsm_state="S3",
        intents=[Intent.SIDE_QUERY],
        language=question.language,
        as_of=datetime.now(UTC),
        corpus_pins=pins,
    )


async def run_all(
    service: RetrievalService, items: list[Question], pins: dict[Collection, str]
) -> list[Outcome]:
    outcomes = []
    for n, question in enumerate(items, start=1):
        started = time.perf_counter()
        selection = await service.select(question.text, eval_context(question, pins))
        outcomes.append(Outcome(question, selection))
        logger.info(
            "%3d/%d %-8s %-18s %d chunks in %.1f s",
            n,
            len(items),
            question.id,
            selection.decision.rule_id,
            len(selection.evidence),
            time.perf_counter() - started,
        )
    return outcomes


def thresholds_for(
    outcomes: list[Outcome],
    bm25_outcomes: list[Outcome] | None,
    pins: dict[Collection, str],
    reranker: TeiModel,
) -> Thresholds:
    """One threshold per snapshot and scoring: the collection's own questions are the positives,
    the unanswerable questions routed to it the negatives. Without BM25-only runs (the bake-off),
    the bm25 thresholds are infinite: that fallback then always abstains."""

    def cut(runs: list[Outcome], domain: Collection) -> float:
        positives: list[float] = []
        negatives: list[float] = []
        for o in runs:
            best = o.best(domain)
            if best is not None and o.question.collection in (domain, None):
                (positives if o.question.collection == domain else negatives).append(best)
        return calibrate(positives, negatives)

    return Thresholds(
        degraded_factor=DEGRADED_FACTOR,
        reranker=Stamp(model_id=reranker.model_id, model_sha=reranker.model_sha),
        snapshots={
            pins[d]: SnapshotThresholds(
                rerank=cut(outcomes, d),
                bm25=cut(bm25_outcomes, d) if bm25_outcomes is not None else math.inf,
            )
            for d in COLLECTIONS
        },
    )


@dataclass(frozen=True)
class Report:
    lines: list[str]
    recall: dict[Collection, float]
    precision: dict[Collection, float]
    abstention: float
    # Step 23 (language parity): the means per (collection, language) and the abstention
    # accuracy per language, over the same decisions.
    by_language: dict[tuple[Collection, str], tuple[float, float]] = field(default_factory=dict)
    abstention_by_language: dict[str, float] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return (
            all(v >= GATES["recall"] for v in self.recall.values())
            and all(v >= GATES["precision"] for v in self.precision.values())
            and self.abstention >= GATES["abstention"]
        )


def report(service: RetrievalService, outcomes: list[Outcome], thresholds: Thresholds) -> Report:
    decided = [(o, service.decide(o.selection, thresholds)) for o in outcomes]
    rows: dict[tuple[Collection, str], list[tuple[float, float, bool]]] = defaultdict(list)
    lines = [f"{'collection':<11}{'language':<9}{'n':>3}  recall@8  precision@8  answered"]
    misses = []
    for outcome, result in decided:
        q = outcome.question
        if q.collection is None:
            continue
        r = recall_at_k(q.gold, outcome.ranked)
        p = context_precision_at_k(q.gold, outcome.ranked)
        for key in ((q.collection, "all"), (q.collection, q.language)):
            rows[key].append((r, p, not result.abstained))
        if r < 1 or result.abstained:
            rule = outcome.selection.decision.rule_id
            abstained = f", abstained {result.abstain_reason}" if result.abstained else ""
            misses.append(f"  {q.id}: recall {r:.2f}, precision {p:.2f}, {rule}{abstained}")
    recall, precision = {}, {}
    by_language: dict[tuple[Collection, str], tuple[float, float]] = {}
    for domain in COLLECTIONS:
        for language in ("all", "en", "hi", "hi-Latn"):
            got = rows.get((domain, language), [])
            if not got:
                continue
            mean_r = sum(r for r, _, _ in got) / len(got)
            mean_p = sum(p for _, p, _ in got) / len(got)
            answered = sum(a for _, _, a in got)
            if language == "all":
                recall[domain], precision[domain] = mean_r, mean_p
            else:
                by_language[(domain, language)] = (mean_r, mean_p)
            lines.append(
                f"{domain:<11}{language:<9}{len(got):>3}  {mean_r:8.3f}  {mean_p:11.3f}"
                f"  {answered:>3}/{len(got)}"
            )
    correct = sum((o.question.collection is None) == r.abstained for o, r in decided)
    per_language: dict[str, list[bool]] = defaultdict(list)
    for o, decision in decided:
        per_language[o.question.language].append(
            (o.question.collection is None) == decision.abstained
        )
    unanswerable = [r for o, r in decided if o.question.collection is None]
    abstention = correct / len(decided)
    lines.append(
        f"abstention accuracy {abstention:.3f} ({correct}/{len(decided)}); unanswerable"
        f" abstained {sum(r.abstained for r in unanswerable)}/{len(unanswerable)} (§5.2 subset)"
    )
    wrong = [
        f"  {o.question.id}: {o.selection.decision.rule_id}, best score"
        f" {max((s.score or 0 for s in o.selection.evidence), default=0):.3f}"
        for o, r in decided
        if o.question.collection is None and not r.abstained
    ]
    if misses:
        lines += ["misses:", *misses]
    if wrong:
        lines += ["answered, should have abstained:", *wrong]
    return Report(
        lines,
        recall,
        precision,
        abstention,
        by_language,
        {lang: sum(ok) / len(ok) for lang, ok in per_language.items()},
    )


def _gate_lines(result: Report) -> list[str]:
    measured = {
        "recall": min(result.recall.values()),
        "precision": min(result.precision.values()),
        "abstention": result.abstention,
    }
    return [
        f"gate {name} >= {GATES[name]:.2f}: {value:.3f} {_verdict(value >= GATES[name])}"
        for name, value in measured.items()
    ]


def _verdict(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _summary(result: Report) -> list[str]:
    """The table and the abstention line, without the per-question detail."""
    end = next((i for i, line in enumerate(result.lines) if line.endswith(":")), len(result.lines))
    return result.lines[:end]


def write_thresholds(
    path: Path, thresholds: Thresholds, live: Report, degraded: Report, pins: dict[Collection, str]
) -> None:
    """Keeps older snapshots' entries while the reranker is unchanged (sessions pinned to them
    still need them); a new reranker invalidates every older entry."""
    snapshots = dict(thresholds.snapshots)
    if path.exists():
        previous = Thresholds.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        if previous.reranker == thresholds.reranker:
            snapshots = {**previous.snapshots, **snapshots}
        else:
            logger.warning("a new reranker: dropping thresholds of %s", sorted(previous.snapshots))
    body = thresholds.model_copy(update={"snapshots": dict(sorted(snapshots.items()))})
    header = [
        "# Sufficiency-gate thresholds (TDD §2.4 step 5), keyed by corpus snapshot: written by",
        "# `make calibrate-retrieval`, never by hand. rerank is on the reranker's 0-1 score, bm25",
        "# on Qdrant's BM25 score (the rerank-down fallback); degraded retrieval multiplies either",
        "# by degraded_factor. Valid only for the reranker below: another one needs recalibration.",
        f"# Calibrated {datetime.now(UTC).date()} on {', '.join(sorted(pins.values()))},",
        "# with the golden answerable sets plus content/golden/unanswerable.yaml. Metrics at these",
        "# thresholds:",
        *(f"#   {line}" for line in _summary(live)),
        "# BM25-only (embed and rerank down, degraded_factor applied):",
        *(f"#   {line}" for line in _summary(degraded)),
    ]
    document = yaml.safe_dump(body.model_dump(mode="json"), sort_keys=False)
    path.write_text("\n".join(header) + "\n" + document, encoding="utf-8")


async def main_async(command: str) -> int:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    if settings.pg_dsn_app is None:
        logger.error("set SS_PG_DSN_APP (app_rw on the database holding the corpus snapshots)")
        return 2
    items, pins = questions()
    thresholds_path = KB_CONFIG / "thresholds.yaml"
    with ConnectionPool(settings.pg_dsn_app.get_secret_value(), min_size=1) as pool:
        meta = partial(load_snapshot_meta, pool)
        qdrant = AsyncQdrantClient(url=settings.qdrant_url)
        async with Gateway(settings) as gateway:
            if command == "bakeoff-rerank":
                return await bakeoff_rerank(settings, qdrant, meta, items, pins)
            tei = CachedRerank(gateway)
            live = RetrievalService(tei, qdrant, meta)
            bm25_only = RetrievalService(Bm25Only(), qdrant, meta)
            outcomes = await run_all(live, items, pins)
            bm25_outcomes = await run_all(bm25_only, items, pins)
            if command == "calibrate":
                thresholds = thresholds_for(outcomes, bm25_outcomes, pins, await tei.rerank_model())
            elif live.thresholds is None:
                logger.error("no content/kb/thresholds.yaml: run make calibrate-retrieval first")
                return 2
            else:
                thresholds = live.thresholds
            result = report(live, outcomes, thresholds)
            degraded = report(bm25_only, bm25_outcomes, thresholds)
    for line in result.lines + _gate_lines(result):
        logger.info("%s", line)
    logger.info("BM25-only (degraded; information, not gated):")
    for line in degraded.lines:
        logger.info("%s", line)
    if command == "calibrate":
        write_thresholds(thresholds_path, thresholds, result, degraded, pins)
        logger.info("wrote %s", thresholds_path.relative_to(REPO))
        return 0
    return 0 if result.passed else 1


async def bakeoff_rerank(
    settings: Settings,
    qdrant: AsyncQdrantClient,
    meta: Callable[[str], SnapshotMeta | None],
    items: list[Question],
    pins: dict[Collection, str],
) -> int:
    """TDD §2.2 / guide 7b: both rerankers on one candidate pool (the same embed, search and
    filters feed both), each calibrated on its own scores; then p95 latency at 120 candidates.
    SS_BAKEOFF_SIDES=baseline or challenger runs one side, for a Docker VM that cannot hold both
    rerankers at once."""
    urls = {
        "baseline": settings.tei_rerank_url,
        "challenger": os.environ.get("SS_BAKEOFF_RERANK_URL", "http://127.0.0.1:8084"),
    }
    sides = os.environ.get("SS_BAKEOFF_SIDES", "baseline,challenger").split(",")
    runs = int(os.environ.get("SS_BAKEOFF_LATENCY_RUNS", "5"))
    documents = await _latency_documents(qdrant, pins)
    rows = []
    for name, url in ((side, urls[side]) for side in sides):
        async with Gateway(settings.model_copy(update={"tei_rerank_url": url})) as gateway:
            tei = CachedRerank(gateway)
            served = await tei.rerank_model()
            service = RetrievalService(tei, qdrant, meta)
            outcomes = await run_all(service, items, pins)
            thresholds = thresholds_for(outcomes, None, pins, served)
            result = report(service, outcomes, thresholds)
            latencies = []
            for _ in range(runs):
                started = time.perf_counter()
                await gateway.rerank("What does the policy exclude?", documents)
                latencies.append(time.perf_counter() - started)
        p95 = sorted(latencies)[max(0, math.ceil(0.95 * len(latencies)) - 1)]
        rows.append((name, served, result, p95))
        for line in result.lines:
            logger.info("%s: %s", name, line)
    logger.info(
        "%-11s %-50s %7s %7s %7s %7s", "reranker", "model", "recall", "prec", "abst", "p95 s"
    )
    for name, served, result, p95 in rows:
        logger.info(
            "%-11s %-50s %7.3f %7.3f %7.3f %7.1f",
            name,
            f"{served.model_id}@{served.model_sha[:8]}",
            min(result.recall.values()),
            min(result.precision.values()),
            result.abstention,
            p95,
        )
    logger.info(
        "recall and precision: the lowest collection's; p95 over %d runs of %d docs",
        runs,
        len(documents),
    )
    return 0


async def _latency_documents(qdrant: AsyncQdrantClient, pins: dict[Collection, str]) -> list[str]:
    """120 real chunks, as the reranker reads them, 40 from each collection."""
    docs: list[str] = []
    for domain, snapshot_id in pins.items():
        pinned = models.FieldCondition(
            key="snapshot_id", match=models.MatchValue(value=snapshot_id)
        )
        records, _ = await qdrant.scroll(
            f"kb_{domain}",
            scroll_filter=models.Filter(must=[pinned]),
            limit=40,
            with_payload=True,
        )
        docs += [indexed_text(PAYLOAD.validate_python(r.payload)) for r in records]
    return docs


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m surakshasetu.eval.retrieval_metrics")
    parser.add_argument("command", choices=["calibrate", "eval", "bakeoff-rerank"])
    return asyncio.run(main_async(parser.parse_args().command))


if __name__ == "__main__":
    raise SystemExit(main())
