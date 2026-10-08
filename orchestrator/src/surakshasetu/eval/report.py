"""The evaluation report (Step 23): reports/eval-<ts>.json and its markdown summary (the CI
artifact). Counts, ids, ratios and gate verdicts only: no utterance, value or released text."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from surakshasetu.eval.metrics import (
    DEFINITIONS,
    LANGUAGES,
    STEP_DEFINITIONS,
    Gate,
    blocking,
    latency_class,
    p95,
)

# TDD §5.4 / §4.2 / guide Step 23: what the seed sets hold against the production targets.
TARGETS = {
    "golden conversations per state": "about 600 (TDD §5.4)",
    "question-evidence pairs per collection": "1,000 (TDD §5.4)",
    "red-team adversarial turns": "about 1,500 (TDD §4.2)",
    "labelled slot utterances": "none stated; this step's floor 200",
    "labelled intent turns": "none stated; this step's floor 150",
}


def gate_row(g: Gate) -> dict[str, Any]:
    definition, tdd_gate = DEFINITIONS.get(g.kpi) or STEP_DEFINITIONS.get(g.kpi, ("", ""))
    return {
        "gate": g.name,
        "definition": definition,
        "tdd_gate": tdd_gate,
        "op": g.op,
        "threshold": g.threshold,
        "value": g.value,
        "enforced": g.enforced,
        "status": g.status,
        "note": g.note,
        "waiver": g.waiver.model_dump(mode="json") if g.waiver else None,
    }


def red_team_table(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"attacks": 0, "successes": 0, "invalid": 0, **{lang: 0 for lang in LANGUAGES}}
    )
    for r in records:
        for a in r.get("attacks", []):
            row = rows[a["category"]]
            row["attacks"] += 1
            row[a["language"]] += 1
            row["successes"] += bool(a["successes"])
            row["invalid"] += bool(a["invalid"])
            row.setdefault("stoppers", set()).add(a["stopper"])
    return [
        {"category": k, **v, "stoppers": sorted(v.get("stoppers", ()))}
        for k, v in sorted(rows.items())
    ]


def state_table(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    turns: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in records:
        if "error" in r:
            continue
        for t in r.get("turns", []):
            turns[latency_class(t)].append(t)
    return [
        {
            "class": name,
            "turns": len(ts),
            "samples": sum(bool(t.get("sample")) for t in ts),
            "p95_ms": p95([t["latency_ms"] for t in ts if t.get("sample")]),
        }
        for name, ts in sorted(turns.items())
    ]


def bundle_table(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    turns: dict[str, int] = defaultdict(int)
    conversations: dict[str, set[str]] = defaultdict(set)
    for r in records:
        for t in r.get("turns", []):
            bundle = str(t.get("bundle"))
            turns[bundle] += 1
            conversations[bundle].add(r["id"])
    return [
        {"bundle": b, "turns": turns[b], "conversations": len(conversations[b])}
        for b in sorted(turns)
    ]


def suite_table(records: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    suites: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for r in records:
        suites[(r.get("kind", "?"), r.get("suite") or "scripted")].append(bool(r.get("passed")))
    return [
        {"kind": k, "suite": s, "conversations": len(v), "passed": sum(v)}
        for (k, s), v in sorted(suites.items())
    ]


def assemble(
    *,
    mode: str,
    stamp: str,
    gates: Sequence[Gate],
    records: Sequence[Mapping[str, Any]],
    sections: Mapping[str, Any],
) -> dict[str, Any]:
    failing = blocking(gates)
    return {
        "mode": mode,
        "generated": stamp,
        "result": "FAIL" if failing else "PASS",
        "blocking": [g.name for g in failing],
        "gates": [gate_row(g) for g in gates],
        "suites": suite_table(records),
        "red_team": red_team_table(records),
        "states": state_table(records),
        "bundles": bundle_table(records),
        "targets": TARGETS,
        **sections,
    }


def _value(v: float | None, op: str) -> str:
    if v is None:
        return "n/a"
    return f"{v:.0f}" if op == "==" or v >= 100 else f"{v:.3f}"


def markdown(report: Mapping[str, Any]) -> str:
    lines = [
        f"# SurakshaSetu evaluation ({report['mode']}, {report['generated']})",
        "",
        f"**Result: {report['result']}**"
        + (f" (blocking: {', '.join(report['blocking'])})" if report["blocking"] else ""),
        "",
        "Enforced gates block the release; INFO rows are computed but not gated in this mode"
        " (against the stub, model-dependent metrics are informational). n/a: nothing measured.",
        "",
        "## Gates",
        "",
        "| Gate | TDD definition | TDD gate | Value | Threshold | Status | Note |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for g in report["gates"]:
        waiver = g["waiver"]
        note = g["note"] + (
            f" Waived ({waiver['owner']}): {waiver['reason']}"
            if waiver and g["status"] == "WAIVED"
            else ""
        )
        lines.append(
            f"| {g['gate']} | {g['definition']} | {g['tdd_gate']} | {_value(g['value'], g['op'])}"
            f" | {g['op']} {g['threshold']:g} | {g['status']} | {note.strip()} |"
        )
    if report["red_team"]:
        total = sum(r["attacks"] for r in report["red_team"])
        got = sum(r["successes"] for r in report["red_team"])
        lines += [
            "",
            f"## Red-team: {got} successes in {total} attack turns",
            "",
            "| Category | Attacks | en | hi | hi-Latn | Successes | Invalid | Stopped by |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |",
            *(
                f"| {r['category']} | {r['attacks']} | {r['en']} | {r['hi']} | {r['hi-Latn']}"
                f" | {r['successes']} | {r['invalid']} | {', '.join(r['stoppers'])} |"
                for r in report["red_team"]
            ),
        ]
    if report["suites"]:
        lines += [
            "",
            "## Conversation suites",
            "",
            "| Kind | Suite | Conversations | Passed |",
            "| --- | --- | --- | --- |",
            *(
                f"| {s['kind']} | {s['suite']} | {s['conversations']} | {s['passed']} |"
                for s in report["suites"]
            ),
        ]
    if report["states"]:
        lines += [
            "",
            "## Per state (latency from the stack run; side questions are their own class)",
            "",
            "| State | Turns | Latency samples | p95 ms |",
            "| --- | --- | --- | --- |",
            *(
                f"| {s['class']} | {s['turns']} | {s['samples']} | {_value(s['p95_ms'], '==')} |"
                for s in report["states"]
            ),
        ]
    if languages := report.get("languages"):
        lines += [
            "",
            "## Per language",
            "",
            "| Metric | en | hi | hi-Latn |",
            "| --- | --- | --- | --- |",
            *(
                f"| {metric} | "
                + " | ".join(_value(values.get(lang), ">=") for lang in LANGUAGES)
                + " |"
                for metric, values in languages.items()
            ),
        ]
    if report["bundles"]:
        lines += [
            "",
            "## Per prompt bundle",
            "",
            "| Bundle | Turns | Conversations |",
            "| --- | --- | --- |",
            *(
                f"| {b['bundle']} | {b['turns']} | {b['conversations']} |"
                for b in report["bundles"]
            ),
        ]
    if counts := report.get("counts"):
        lines += [
            "",
            "## Sets against the TDD's production targets",
            "",
            "| Set | Now | Target |",
            "| --- | --- | --- |",
            *(f"| {k} | {counts.get(k, 'n/a')} | {v} |" for k, v in report["targets"].items()),
        ]
    if notes := report.get("notes"):
        lines += ["", "## Notes", "", *(f"- {n}" for n in notes)]
    return "\n".join(lines) + "\n"


def drafts_summary(drafts: Sequence[Any]) -> dict[str, Any]:
    return {
        "questions": len(drafts),
        "abstained": sum(d.abstained for d in drafts),
        "errors": sum(d.error is not None for d in drafts),
        "claims": sum(len(d.claims) for d in drafts),
        "citations": sum(len(d.pairs) for d in drafts),
        "served_models": sorted({d.served_model for d in drafts if d.served_model}),
    }
