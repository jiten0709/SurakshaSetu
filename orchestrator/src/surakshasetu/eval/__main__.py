"""The evaluation harness (Step 23, TDD §5.2/§5.3):

    python -m surakshasetu.eval run --mode stub --records DIR   (make eval, after the suites)
    python -m surakshasetu.eval run --mode live                 (make eval-live, after D2)

stub: the deterministic gates are enforced (the golden and red-team runs' records, retrieval,
abstention, citation coverage, zero unsupported premiums and disclosures, §5.3, latency, coverage);
the model-dependent metrics (labels, QA, parity, fallback) are computed against the stub and
reported as information. live: the routes must be provisioned (no stub model may answer), and every
§5.2 gate is enforced on the labels, retrieval and QA; the scripted suites cannot run against real
models, so their gates stay with `make eval`.

Writes reports/eval-<ts>.json and reports/eval-<ts>.md. Exit 0 when every enforced gate passes
(or is waived), 1 when one fails, 2 when the run cannot be made (a broken label, record or waiver
file; unprovisioned routes in live mode; no thresholds).
"""

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any

import yaml
from psycopg_pool import ConnectionPool
from pydantic import ValidationError
from qdrant_client import AsyncQdrantClient

from surakshasetu.analysis.models import TurnAnalysis
from surakshasetu.compose.bundle import load_bundle
from surakshasetu.compose.envelope import SessionFacts, build
from surakshasetu.config import Settings
from surakshasetu.domain.client import DomainClient
from surakshasetu.eval import labels, metrics, qa, report, retrieval_metrics
from surakshasetu.gateway import DataClass, Gateway, GatewayUnavailable, Route
from surakshasetu.logging import configure_logging
from surakshasetu.rails.injection import GuardVerdict
from surakshasetu.rails.output import ClaimCheck, load_pack
from surakshasetu.retrieval.service import RetrievalService, load_snapshot_meta
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger("surakshasetu.eval")

REPO = labels.GOLDEN.parents[1]
REPORTS = REPO / "reports"
WAIVERS = REPO / "content" / "eval" / "waivers.yaml"
CONVERSATIONS = labels.GOLDEN / "conversations"
REDTEAM = REPO / "content" / "redteam"
UNPROVISIONED = "eval-live requires provisioned model routes (D2)"


class Unrunnable(Exception):
    """The evaluation cannot be made (exit 2)."""


def expected_ids() -> list[str]:
    """Every golden and red-team conversation id, read from the files (each run writes a record)."""
    ids = [
        yaml.safe_load(p.read_text("utf-8"))["id"]
        for p in [*CONVERSATIONS.glob("*.yaml"), *CONVERSATIONS.glob("*/*.yaml")]
        if not any(part.startswith(".") for part in p.parts)
    ]
    for p in REDTEAM.glob("*.yaml"):
        ids += [c["id"] for c in yaml.safe_load(p.read_text("utf-8"))["conversations"]]
    return ids


def load_records(directory: Path) -> list[dict[str, Any]]:
    if not directory.is_dir():
        raise Unrunnable(f"no records directory {directory}: run the suites first (make eval)")
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            records.append(json.loads(path.read_text("utf-8")))
        except json.JSONDecodeError as exc:
            raise Unrunnable(f"record {path.name} is not JSON") from exc
    return records


async def probe(gateway: Gateway, settings: Settings) -> None:
    """Live mode: every chat route the evaluation uses answers, from a model that is not a stub."""
    bundle = load_bundle(settings.prompt_bundle, env=settings.env)
    ping = [{"role": "user", "content": "Reply with OK."}]
    calls: list[tuple[Route, dict[str, Any]]] = [
        (Route.GUARD_INPUT, {"data_class": DataClass.SELF_HOSTED_RAW, "messages": ping,
                             "response_format": GuardVerdict}),
        (Route.NLU_EXTRACT, {"data_class": DataClass.SELF_HOSTED_RAW, "messages": ping,
                             "response_format": TurnAnalysis}),
        (Route.VERIFY_CLAIMS, {"data_class": DataClass.SELF_HOSTED_RAW, "messages": ping,
                               "response_format": ClaimCheck}),
    ]  # fmt: skip
    for l1, slot in (("side-query", None), ("S1", next(iter(bundle.templates["en-IN"].slots)))):
        envelope = build(
            bundle,
            l1=l1,  # type: ignore[arg-type]
            locale="en-IN",
            user_text="Reply with OK.",
            facts=SessionFacts(language="en"),
            next_slot=slot,
        )
        calls.append(
            (
                envelope.route,
                {
                    "data_class": envelope.data_class,
                    "messages": envelope.messages,
                    "attestation": envelope.attestation,
                },
            )
        )
    for route, kwargs in calls:
        try:
            result = await gateway.call(
                route, session_id=uuid7(), turn_id=uuid7(), fsm_state="S3", **kwargs
            )
        except GatewayUnavailable as exc:
            raise Unrunnable(f"{UNPROVISIONED}: {route.value} {exc.reason}") from exc
        if "stub" in result.served_model.casefold():
            raise Unrunnable(f"{UNPROVISIONED}: {route.value} is served by a stub")


class Calls:
    """Every model call the evaluation makes (route, served model, ms): fallback and live
    latency per route."""

    def __init__(self, gateway: Gateway) -> None:
        self.seen: list[tuple[str, str, float]] = []
        inner = gateway.call

        async def recorded(route: Route, **kwargs: Any) -> Any:
            result = await inner(route, **kwargs)
            self.seen.append((route.value, result.served_model, result.latency_ms))
            return result

        gateway.call = recorded  # type: ignore[method-assign]


async def evaluate(mode: str, records_dir: Path | None, settings: Settings) -> dict[str, Any]:
    live = mode == "live"
    try:
        slot_items, intent_items = labels.load_slots(), labels.load_intents()
        waivers = metrics.load_waivers(WAIVERS)
    except (FileNotFoundError, ValueError, ValidationError) as exc:
        raise Unrunnable(f"a label or waiver file is broken: {exc}") from exc
    records = load_records(records_dir) if records_dir else []
    if settings.pg_dsn_app is None:
        raise Unrunnable("set SS_PG_DSN_APP (app_rw on the database holding the corpus snapshots)")
    items, pins = retrieval_metrics.questions()
    bundle = load_bundle(settings.prompt_bundle, env=settings.env)
    pack = load_pack(settings.output_lexicon)
    with ConnectionPool(settings.pg_dsn_app.get_secret_value(), min_size=1) as pool:
        meta = partial(load_snapshot_meta, pool)
        qdrant = AsyncQdrantClient(url=settings.qdrant_url)
        async with Gateway(settings) as gateway, DomainClient.from_settings(settings) as domain:
            if live:
                await probe(gateway, settings)
            calls = Calls(gateway)
            asked = await labels.asked_slots(domain)
            slot_results = await labels.run_slots(slot_items, gateway, domain, settings, asked)
            intent_results = await labels.run_intents(intent_items, gateway, settings)
            service = RetrievalService(retrieval_metrics.CachedRerank(gateway), qdrant, meta)
            if service.thresholds is None:
                raise Unrunnable("no content/kb/thresholds.yaml: run make calibrate-retrieval")
            outcomes = await retrieval_metrics.run_all(service, items, pins)
            retrieval = retrieval_metrics.report(service, outcomes, service.thresholds)
            drafts = await qa.run_qa(gateway, service, outcomes, service.thresholds, bundle, pack)
        await qdrant.close()
    return {
        "live": live,
        "records": records,
        "waivers": waivers,
        "slots": slot_results,
        "intents": intent_results,
        "retrieval": retrieval,
        "drafts": drafts,
        "calls": calls.seen,
        "rules": asked,
    }


def gates_of(run: dict[str, Any], settings: Settings) -> list[metrics.Gate]:
    live = run["live"]
    slots = run["slots"]
    by_language = {
        lang: metrics.slot_scores(
            (r.item.gold, r.predicted) for r in slots if r.item.language == lang
        )
        for lang in metrics.LANGUAGES
    }
    intents = run["intents"]
    pairs = [({i.value for i in r.item.intents}, r.predicted) for r in intents]
    intents_by_language = {
        lang: metrics.intent_recall(
            ({i.value for i in r.item.intents}, r.predicted)
            for r in intents
            if r.item.language == lang
        )
        for lang in metrics.LANGUAGES
    }
    retrieval: retrieval_metrics.Report = run["retrieval"]
    gates = []
    if not live:
        gates += metrics.conversation_gates(
            run["records"], expected_ids(), settings.eval_primary_models
        )
    gates += metrics.retrieval_gates(
        retrieval.recall,
        retrieval.precision,
        retrieval.abstention,
        retrieval.by_language,
        retrieval.abstention_by_language,
        parity_enforced=live,
    )
    gates += metrics.label_gates(
        metrics.slot_scores((r.item.gold, r.predicted) for r in slots),
        by_language,
        metrics.intent_recall(pairs),
        intents_by_language,
        enforced=live,
    )
    gates += metrics.qa_gates(
        qa.scores(run["drafts"], "claims"), qa.scores(run["drafts"], "pairs"), enforced=live
    )
    if live:
        rate = metrics.fallback_rate(
            [(route, model) for route, model, _ in run["calls"]], settings.eval_primary_models
        )
        note = "" if settings.eval_primary_models else "unmeasured: SS_EVAL_PRIMARY_MODELS is empty"
        gates.append(
            metrics.Gate(
                "latency_fallback:fallback", "latency_fallback", rate, "<=", 0.02, True, note
            )
        )
    return gates


def sections(run: dict[str, Any]) -> dict[str, Any]:
    """The per-language table, set sizes, per-route latency and notes for the report."""
    slots, intents, drafts = run["slots"], run["intents"], run["drafts"]
    retrieval: retrieval_metrics.Report = run["retrieval"]
    per_language: dict[str, dict[str, float | None]] = {}
    for lang in metrics.LANGUAGES:
        scored = metrics.slot_scores(
            (r.item.gold, r.predicted) for r in slots if r.item.language == lang
        )
        per_language.setdefault("slot F1 (micro)", {})[lang] = scored.micro
        per_language.setdefault("money exact", {})[lang] = scored.money_exact
        per_language.setdefault("critical-intent recall", {})[lang] = metrics.intent_recall(
            ({i.value for i in r.item.intents}, r.predicted)
            for r in intents
            if r.item.language == lang
        )["all"]
        per_language.setdefault("abstention", {})[lang] = retrieval.abstention_by_language.get(lang)
    for (collection, lang), (recall, precision) in retrieval.by_language.items():
        per_language.setdefault(f"recall@8 {collection}", {})[lang] = recall
        per_language.setdefault(f"precision@8 {collection}", {})[lang] = precision
    for name, attribute in (("faithfulness", "claims"), ("citation precision", "pairs")):
        per_language[name] = {k: v for k, v in qa.scores(drafts, attribute).items() if k != "all"}
    routes: dict[str, list[float]] = {}
    for route, _, ms in run["calls"]:
        routes.setdefault(route, []).append(ms)
    per_slot = metrics.slot_scores((r.item.gold, r.predicted) for r in slots)
    misses = [
        {"id": r.item.id, "slot": slot, "kind": _miss(r.item.gold, r.predicted, slot)}
        for r in slots
        for slot in sorted(set(r.item.gold) | set(r.predicted))
        if _miss(r.item.gold, r.predicted, slot)
    ]
    critical = set(metrics.CRITICAL_INTENTS)
    missed_intents = [
        {
            "id": r.item.id,
            "missed": sorted({i.value for i in r.item.intents} & critical - r.predicted),
        }
        for r in intents
        if {i.value for i in r.item.intents} & critical - r.predicted
    ]
    golden = [r for r in run["records"] if r.get("kind") == "golden"]
    per_state = metrics.conversations_per_state(golden)
    return {
        "languages": per_language,
        "slots": {"per_slot": per_slot.per_slot, "counts": per_slot.counts, "misses": misses},
        "intents": {"missed_critical": missed_intents},
        "retrieval": {"lines": retrieval.lines},
        "qa": report.drafts_summary(drafts),
        "route_latency_p95_ms": {k: metrics.p95(v) for k, v in sorted(routes.items())},
        "counts": {
            "golden conversations per state": ", ".join(
                f"{s} {per_state.get(s, 0)}" for s in metrics.JOURNEY_STATES
            ),
            "question-evidence pairs per collection": sum(
                1 for q in retrieval_metrics.questions()[0] if q.collection
            )
            // 3,
            "red-team adversarial turns": sum(
                len(r.get("attacks", [])) for r in run["records"] if r.get("kind") == "redteam"
            ),
            "labelled slot utterances": len(slots),
            "labelled intent turns": len(intents),
        },
        "notes": [
            "Model-dependent metrics are informational against the stub (make eval) and enforced"
            " by make eval-live once D2 provisions routes.",
            "Faithfulness and citation precision follow the RAGAS definitions with verify-claims"
            " as the judge (no ragas library, decided 2026-10-06); against the stub the drafts"
            " state no facts, so they read n/a.",
            "Fallback: OmniRoute 3.8.50 reports no hop count; a fallback is a call served by a"
            " model other than its route's primary (SS_EVAL_PRIMARY_MODELS).",
            "Abstention accuracy is Step 12's reading (all 70 golden decisions); the TDD's"
            " unanswerable subset is in the retrieval lines.",
            "Live samples are small: one miss among about 60 critical intents fails 0.995.",
        ],
    }


def _miss(gold: dict[str, Any], predicted: dict[str, Any], slot: str) -> str:
    if slot not in predicted:
        return "missing"
    if slot not in gold:
        return "extra"
    same = metrics.canonical(slot, gold[slot]) == metrics.canonical(slot, predicted[slot])
    return "" if same else "wrong"


def write(body: dict[str, Any], stamp: str) -> tuple[Path, Path]:
    REPORTS.mkdir(exist_ok=True)
    stem = REPORTS / f"eval-{stamp}"
    json_path, md_path = stem.with_suffix(".json"), stem.with_suffix(".md")
    json_path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), "utf-8")
    md_path.write_text(report.markdown(body), "utf-8")
    (REPORTS / "eval-latest.md").write_text(md_path.read_text("utf-8"), "utf-8")
    return json_path, md_path


async def main_async(mode: str, records_dir: Path | None) -> int:
    settings = Settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    try:
        run = await evaluate(mode, records_dir, settings)
        gates = metrics.apply_waivers(
            gates_of(run, settings), run["waivers"], datetime.now(UTC).date()
        )
    except Unrunnable as exc:
        logger.error("%s", exc)
        return 2
    expired = metrics.expired(run["waivers"], datetime.now(UTC).date())
    body = report.assemble(
        mode=mode,
        stamp=stamp,
        gates=gates,
        expired=expired,
        records=run["records"],
        sections=sections(run),
    )
    json_path, md_path = write(body, stamp)
    for g in metrics.blocking(gates):
        logger.warning("gate %s failed: %s (%s %s)", g.name, g.value, g.op, g.threshold)
    for w in expired:
        logger.warning("waiver for %s expired on %s", w.gate, w.expires)
    logger.info("evaluation %s: %s; wrote %s and %s", mode, body["result"], json_path, md_path)
    return 0 if body["result"] == "PASS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m surakshasetu.eval")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run")
    run.add_argument("--mode", choices=["stub", "live"], required=True)
    run.add_argument("--records", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.mode == "stub" and args.records is None:
        parser.error("--mode stub needs --records (the golden and red-team runs' records)")
    return asyncio.run(main_async(args.mode, args.records))


if __name__ == "__main__":
    raise SystemExit(main())
