import json
import logging
from pathlib import Path

import pytest
import respx

from surakshasetu.domain.client import DomainClient
from surakshasetu.logging import (
    JsonFormatter,
    _TextFormatter,
    configure_logging,
    request_id_ctx,
    session_id_ctx,
)


def test_record_is_one_json_line_with_context_and_no_extras() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "turn %s done", (3,), None)
    record.body = {"message": "my PAN is ABCDE1234F"}  # what extra={"body": ...} does

    request_token = request_id_ctx.set("req-1")
    session_token = session_id_ctx.set("sess-1")
    try:
        line = JsonFormatter().format(record)
    finally:
        request_id_ctx.reset(request_token)
        session_id_ctx.reset(session_token)

    assert "\n" not in line
    entry = json.loads(line)
    assert entry["msg"] == "turn 3 done"
    assert entry["request_id"] == "req-1"
    assert entry["session_id"] == "sess-1"
    assert "ABCDE1234F" not in line


# --- configure_logging: per-subsystem files, JSON stdout, no leaks -----------------------------
PINCODE = "400001"


def _all_text(log_dir: Path) -> str:
    return "".join(p.read_text() for p in log_dir.iterdir())


def _lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.mark.usefixtures("restore_logging")
def test_records_route_to_one_json_file_per_subsystem(tmp_path: Path) -> None:
    configure_logging("INFO", tmp_path)
    configure_logging("INFO", tmp_path)  # a second call must not stack handlers

    logging.getLogger("surakshasetu.domain.client").debug("domain getVersions -> 200")
    logging.getLogger("surakshasetu.api.app").info("app ready")
    logging.getLogger("uvicorn.error").info("started")
    logging.getLogger("surakshasetu.api.app").info("slot", extra={"pincode": PINCODE})

    assert sorted(p.name for p in tmp_path.iterdir()) == ["api.log", "domain.log", "lib.log"]
    assert [e["msg"] for e in _lines(tmp_path / "domain.log")] == ["domain getVersions -> 200"]
    assert [e["msg"] for e in _lines(tmp_path / "api.log")] == ["app ready", "slot"]
    assert [e["msg"] for e in _lines(tmp_path / "lib.log")] == ["started"]
    assert PINCODE not in (tmp_path / "api.log").read_text()


@pytest.mark.usefixtures("restore_logging")
def test_stdout_is_json_by_default_and_debug_stays_out_of_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("INFO", tmp_path)

    logging.getLogger("surakshasetu.api.app").debug("file only")
    logging.getLogger("surakshasetu.api.app").info("both")

    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["msg"] for line in out] == ["both"]


def test_text_format_drops_extras_and_colours_only_on_request() -> None:
    record = logging.LogRecord("t", logging.WARNING, __file__, 1, "degraded", (), None)
    record.pincode = PINCODE

    plain = _TextFormatter(color=False).format(record)
    coloured = _TextFormatter(color=True).format(record)

    assert "degraded" in plain and "\033[" not in plain
    assert coloured.startswith("\033[33m")
    assert PINCODE not in plain + coloured


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
@respx.mock(base_url="http://domain.test")
async def test_a_real_domain_call_leaks_no_pincode_anywhere(
    respx_mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # httpx logs the full URL at INFO; configure_logging must silence it on stdout and in files.
    configure_logging("DEBUG", tmp_path)
    respx_mock.get(f"/v1/reference/pincodes/{PINCODE}").respond(
        200,
        json={
            "pincode": PINCODE,
            "district": "Mumbai",
            "state": "MH",
            "serviceable": True,
            "is_dummy": True,
        },
    )

    async with DomainClient("http://domain.test", "t0ken") as client:
        await client.get_pincode(PINCODE)

    stdout = capsys.readouterr().out
    files = _all_text(tmp_path)
    assert "getPincode" in stdout and "getPincode" in files
    for leaked in (PINCODE, "/v1/", "t0ken"):
        assert leaked not in stdout + files
