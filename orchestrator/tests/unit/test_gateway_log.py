"""Bug #43's gate (decision log §2, 2026-11-17) over omniroute-live's saved log. No network, no
Docker.

omniroute_live_probe.txt holds 22 lines of the payload-rules probe's saved log
(~/surakshasetu-evidence/2026-11-16-payload-rules-probe/live-gateway-20261117T110113Z.log, lines 1,
9, 232, 262, 263, 273-275, 287, 334, 435, 436, 439, 460-462, 467, 477, 479, 480, 486 and 487),
extracted with `sed -n`, byte for byte and in order: text lines, an ANSI-coloured error, a stack
trace whose line ends in "{", other tags (one event at level 40), the four REASONING_SANITIZE
events, and the request that answered 200 after the clamp.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from surakshasetu.eval import __main__ as cli
from surakshasetu.eval import gateway_log, report
from surakshasetu.eval.metrics import Gate

PROBE = (Path(__file__).parent / "omniroute_live_probe.txt").read_text("utf-8")
LINES = PROBE.removesuffix("\n").split("\n")
SANITIZED = [11, 12, 18, 21]  # the probe's lines 435, 436, 477 and 486
CLEAN = [line for n, line in enumerate(LINES, 1) if n not in SANITIZED]
PREFIX = LINES[3][: LINES[3].index("{")]  # "omniroute-live-1  | <timestamp> "
PROBED = ["guard-input", "nlu-extract", "verify-claims"]  # what the self-hosted probe pings
IMAGE = gateway_log.VERIFIED_IMAGE
STAMP = "20261117T110113Z"
SENTINEL = "Zebrafinch-43"


def log(tmp_path: Path, content: list[str] | bytes | None, name: str = "live.log") -> Path:
    path = tmp_path / name
    if isinstance(content, list):
        content = "".join(f"{line}\n" for line in content).encode()
    if content is not None:
        path.write_bytes(content)
    return path


def test_the_probes_four_sanitizer_events_are_found_and_nothing_else(tmp_path: Path) -> None:
    assert len(LINES) == 22
    found = gateway_log.scan(log(tmp_path, LINES))
    assert (found.sanitized, found.problem) == (SANITIZED, "")
    assert found.requests == {
        "guard-input": 1,
        "verify-claims": 1,
        "nlu-extract": 1,
        "summarise": 1,
    }
    gate = gateway_log.gate(log(tmp_path, LINES), PROBED, IMAGE)
    assert (gate.status, gate.value) == ("FAIL", 4.0)
    assert "4 REASONING_SANITIZE events, the first on line 11" in gate.note


def test_a_sanitizer_event_fails_even_when_the_request_then_succeeded(tmp_path: Path) -> None:
    # Line 18 is the clamp on the next summarise request; lines 19 and 20 show it answered 200.
    assert "succeeded" in LINES[18] and r"\"status\":200" in LINES[19]
    gate = gateway_log.gate(log(tmp_path, LINES[:20]), PROBED, IMAGE)
    assert (gate.status, gate.value) == ("FAIL", 3.0)


def test_text_lines_and_other_tags_pass_a_log_with_the_runs_traffic(tmp_path: Path) -> None:
    # "clamped" in a message under another tag, and the tag's name in a text line, are not hits.
    other = LINES[10].replace('"tag":"REASONING_SANITIZE"', '"tag":"COMBO"')
    text = PREFIX + "[REASONING_SANITIZE] clamped reasoning_effort none → low (learned)"
    assert other != LINES[10] and "clamped" in other
    gate = gateway_log.gate(log(tmp_path, [*CLEAN, other, text]), PROBED, IMAGE)
    assert (gate.status, gate.value) == ("PASS", 0.0)
    assert "chat requests guard-input 1, nlu-extract 1, verify-claims 1." in gate.note


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "missing"),
        (b"", "empty"),
        (b"\n \n", "empty"),
        (b"\xff", "unreadable (UnicodeDecodeError)"),
        ([*CLEAN, LINES[3][:-2]], f"line {len(CLEAN) + 1} does not parse as JSON"),
        (  # compose's stderr is merged into the saved log (2>&1)
            [*CLEAN, 'WARN[0000] The "X" variable is not set. Defaulting to a blank string.'],
            f"line {len(CLEAN) + 1} is not an omniroute-live log line",
        ),
        (  # info-level events suppressed: the sanitizer events (level 30) would vanish too
            [line for line in LINES if '"level":30' not in line],
            "no request on guard-input, nlu-extract, verify-claims",
        ),
        ([line for line in CLEAN if "| nlu-extract |" not in line], "no request on nlu-extract"),
    ],
)
def test_a_log_that_cannot_vouch_for_the_run_fails_closed(
    tmp_path: Path, content: list[str] | bytes | None, reason: str
) -> None:
    gate = gateway_log.gate(log(tmp_path, content), PROBED, IMAGE)
    assert (gate.status, gate.value) == ("FAIL", None)
    assert f"fails closed: {reason}." in gate.note


@pytest.mark.parametrize(
    ("routes", "image", "reason"),
    [
        ([], IMAGE, "the report names no probed routes"),
        (PROBED, None, "omniroute-live's image could not be read"),
        (PROBED, IMAGE.replace("3.8.50", "3.9.0"), "omniroute-live's image is not VERIFIED_IMAGE"),
    ],
)
def test_no_probed_routes_or_another_image_fails_closed(
    tmp_path: Path, routes: list[str], image: str | None, reason: str
) -> None:
    gate = gateway_log.gate(log(tmp_path, CLEAN), routes, image)
    assert (gate.status, gate.value) == ("FAIL", None)
    assert f"fails closed: {reason}." in gate.note


def test_the_scan_goes_on_after_a_problem(tmp_path: Path) -> None:
    found = gateway_log.scan(log(tmp_path, ["not a log line", *LINES]))
    assert found.problem == "line 1 is not an omniroute-live log line"
    assert found.sanitized == [n + 1 for n in SANITIZED]


@pytest.fixture
def reports(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """The report a self-hosted run wrote under STAMP, in a scratch reports directory."""
    monkeypatch.setattr(cli, "REPORTS", tmp_path / "reports")
    passing = Gate("suites", "suites", 1.0, ">=", 1.0, True)
    body = report.assemble(
        mode="live:self-hosted",
        stamp=STAMP,
        gates=[passing],
        records=[],
        sections={"probed_routes": PROBED},
        scales={},
    )
    cli.write(body, STAMP)
    return tmp_path / "reports"


def recorded(reports: Path) -> dict[str, Any]:
    body: dict[str, Any] = json.loads((reports / f"eval-{STAMP}.json").read_text("utf-8"))
    return body


def gate_log(path: Path, stamp: str = STAMP) -> int:
    return cli.main(["gateway-log", "--log", str(path), "--stamp", stamp])


@pytest.mark.usefixtures("restore_logging")
def test_the_gate_is_recorded_in_the_runs_report(tmp_path: Path, reports: Path) -> None:
    assert gate_log(log(tmp_path, CLEAN, "clean.log")) == 0  # the real compose image
    body = recorded(reports)
    assert body["result"] == "PASS"
    assert (body["gates"][-1]["gate"], body["gates"][-1]["status"]) == (gateway_log.NAME, "PASS")
    assert gate_log(log(tmp_path, LINES, "dirty.log")) == 1
    body = recorded(reports)
    assert [g["gate"] for g in body["gates"]] == ["suites", gateway_log.NAME]  # replaced
    assert (body["result"], body["blocking"]) == ("FAIL", [gateway_log.NAME])
    markdown = (reports / "eval-latest.md").read_text("utf-8")
    assert f"**Result: FAIL** (blocking: {gateway_log.NAME})" in markdown
    assert markdown == (reports / f"eval-{STAMP}.md").read_text("utf-8")


@pytest.mark.usefixtures("restore_logging")
def test_the_gate_reads_the_routes_and_the_image_the_run_used(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reports: Path
) -> None:
    clean, real = log(tmp_path, CLEAN), cli.COMPOSE
    other = IMAGE.replace("3.8.50", "3.9.0")
    compose = tmp_path / "compose.yaml"
    compose.write_text(f"services:\n  omniroute-live:\n    image: {other}\n", "utf-8")
    monkeypatch.setattr(cli, "COMPOSE", compose)
    assert gate_log(clean) == 1
    assert "is not VERIFIED_IMAGE" in recorded(reports)["gates"][-1]["note"]
    monkeypatch.setattr(cli, "COMPOSE", tmp_path / "absent.yaml")
    assert gate_log(clean) == 1
    assert "image could not be read" in recorded(reports)["gates"][-1]["note"]
    monkeypatch.setattr(cli, "COMPOSE", real)
    assert gate_log(clean) == 0
    body = recorded(reports)
    del body["probed_routes"]  # a report from before probed_routes, or a broken one
    (reports / f"eval-{STAMP}.json").write_text(json.dumps(body), "utf-8")
    assert gate_log(clean) == 1
    assert "the report names no probed routes" in recorded(reports)["gates"][-1]["note"]


@pytest.mark.usefixtures("restore_logging", "reports")
def test_without_its_report_the_gate_exits_2(tmp_path: Path) -> None:
    assert gate_log(log(tmp_path, CLEAN), "20261117T000000Z") == 2


@pytest.mark.parametrize("command", [["gateway-log", "--log", "x.log"], ["run", "--mode", "live"]])
def test_a_stamp_that_is_not_a_report_stamp_is_refused(command: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main([*command, "--stamp", "../eval-x"])
    assert exited.value.code == 2


@pytest.mark.usefixtures("restore_logging")
def test_no_log_message_reaches_the_logs_or_the_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reports: Path
) -> None:
    monkeypatch.setenv("SS_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SS_LOG_LEVEL", "DEBUG")
    planted = [
        LINES[10].replace("ollama/ss-probe-h", SENTINEL),  # a sanitizer event's message
        LINES[3].replace("1 msgs", SENTINEL),  # the HTTP path's last field
        PREFIX + SENTINEL,  # a text line
    ]
    assert all(SENTINEL in line for line in planted)
    assert gate_log(log(tmp_path, [*CLEAN, *planted])) == 1
    logs = "".join(p.read_text() for p in sorted((tmp_path / "logs").glob("*.log")))
    assert f"gate {gateway_log.NAME} failed" in logs  # the positive control
    written = "".join(p.read_text() for p in sorted(reports.iterdir()))
    assert SENTINEL not in logs and SENTINEL not in written
