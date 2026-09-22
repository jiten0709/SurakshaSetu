"""JSON-lines logging with request and session ids taken from contextvars.

The formatter emits a fixed set of fields and drops anything passed via ``extra=``, so a request
body or slot value cannot reach the logs that way. Never format one into the message either.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_ctx: ContextVar[str | None] = ContextVar("session_id", default=None)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
            "session_id": session_id_ctx.get(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # Route uvicorn's own loggers through the root JSON handler.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
