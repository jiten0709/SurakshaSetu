import json
import logging

from surakshasetu.logging import JsonFormatter, request_id_ctx, session_id_ctx


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
