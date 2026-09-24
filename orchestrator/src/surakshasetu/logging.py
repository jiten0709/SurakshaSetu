"""JSON-lines logging with request and session ids taken from contextvars.

The formatters emit a fixed set of fields and drop anything passed via ``extra=``, so a request
body or slot value cannot reach the logs that way. Never format one into the message either.

stdout is always JSON unless SS_LOG_FORMAT=text asks for a coloured line for local reading. With
SS_LOG_DIR set, every record also lands, at DEBUG, in one rotating JSON file per top-level package:
surakshasetu.domain.client -> domain.log; anything not ours (uvicorn, langgraph, ...) -> lib.log.
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Literal

request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
session_id_ctx: ContextVar[str | None] = ContextVar("session_id", default=None)

FILE_LEVEL = logging.DEBUG
MAX_BYTES = 5_000_000  # per file before it rolls; BACKUP_COUNT rolled files are kept
BACKUP_COUNT = 3
# httpx logs every request URL at INFO, query string included, and our paths and queries carry
# customer data (/v1/reference/pincodes/{pincode}, occupations?q=). Pinning it is a PII guard.
NOISY_LOGGERS = ("httpx", "httpx2", "httpcore")  # httpx2: langsmith, via langgraph

_PACKAGE = "surakshasetu"
_COLORS = {
    "DEBUG": "\033[36m",
    "INFO": "\033[32m",
    "WARNING": "\033[33m",
    "ERROR": "\033[31m",
    "CRITICAL": "\033[1;31m",
}


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_ctx.get(),
            "session_id": session_id_ctx.get(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    """The JsonFormatter's fields as one readable line. Colours the string, never the record."""

    def __init__(self, color: bool) -> None:
        super().__init__()
        self._color = color

    def format(self, record: logging.LogRecord) -> str:
        line = (
            f"{_timestamp(record)} {record.levelname:<8} {record.name} "
            f"req={request_id_ctx.get() or '-'} sess={session_id_ctx.get() or '-'} "
            f"{record.getMessage()}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        color = _COLORS.get(record.levelname) if self._color else None
        return f"{color}{line}\033[0m" if color else line


class _SubsystemFiles(logging.Handler):
    """One rotating JSON file per top-level package, opened on first use."""

    def __init__(self, log_dir: Path) -> None:
        super().__init__(FILE_LEVEL)
        self._dir = log_dir
        self._files: dict[str, RotatingFileHandler] = {}

    def emit(self, record: logging.LogRecord) -> None:
        parts = record.name.split(".")
        name = parts[1] if parts[0] == _PACKAGE and len(parts) > 1 else "lib"
        handler = self._files.get(name)
        if handler is None:  # handle() holds self.lock, so this cannot race
            handler = RotatingFileHandler(
                self._dir / f"{name}.log",
                maxBytes=MAX_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            handler.setFormatter(JsonFormatter())
            self._files[name] = handler
        handler.emit(record)

    def close(self) -> None:
        for handler in self._files.values():
            handler.close()
        super().close()


def configure_logging(
    level: str, log_dir: Path | None = None, fmt: Literal["json", "text"] = "json"
) -> None:
    """Configure the root logger. Idempotent: a second call replaces the first's handlers."""
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(level)
    console.setFormatter(
        _TextFormatter(color=sys.stdout.isatty()) if fmt == "text" else JsonFormatter()
    )
    handlers: list[logging.Handler] = [console]
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(_SubsystemFiles(log_dir))
    logging.basicConfig(level=level, handlers=handlers, force=True)
    # Our own DEBUG reaches the files; third-party loggers stay at the console level.
    logging.getLogger(_PACKAGE).setLevel(FILE_LEVEL if log_dir is not None else logging.NOTSET)
    for name in NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    # Route uvicorn's own loggers through the root handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
