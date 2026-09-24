"""
Standardized logging for TestRAGic.

Strategy: one log file per top-level source directory, not per module.
Every module under src/agents/ calls get_logger(__name__, log_file="agents.log"),
so __name__ keeps the module visible in each line while the file groups by subsystem.
Console and file levels are configured independently in the block below.

Streamlit re-runs the whole script on every interaction and serves concurrent
sessions from one process, so two things matter here that don't in a CLI:
handlers must not stack across re-runs (the `if logger.handlers` guard), and
lines from different runs interleave (the `run=` field -- see `run_context`).
"""

import logging
import os
import sys
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Configuration ────────────────────────────────────────────────────────────
FILE_MODE = "w"                 # "a" keeps history across runs, "w" starts each run fresh
CONSOLE_LEVEL = logging.ERROR    # what you see in the terminal
FILE_LEVEL = logging.DEBUG      # what lands in logs/*.log
MAX_BYTES = 5_000_000           # per file, before it rolls
BACKUP_COUNT = 3                # so a long-lived server keeps ~20MB per subsystem
LOG_DIR = Path(os.getenv("LOG_DIR") or Path(__file__).resolve().parents[2] / "logs")

# Chatty at INFO and none of it ours. yt_dlp/pytube narrate every download fragment,
# sentence_transformers/torch announce model loads, watchdog fires per file event.
NOISY_LOGGERS = ("httpx", "httpcore", "openai", "urllib3", "yt_dlp", "pytube",
                 "sentence_transformers", "transformers", "torch", "faiss",
                 "watchdog", "matplotlib", "PIL", "streamlit", "uvicorn.access")

# LOG_LEVEL=DEBUG streamlit run run_app.py  → louder console for one run, file is unaffected
_env_level = os.getenv("LOG_LEVEL")
if _env_level:
    CONSOLE_LEVEL = getattr(logging, _env_level.upper(), CONSOLE_LEVEL)

for _noisy in NOISY_LOGGERS:
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_FORMAT = "%(asctime)s | %(levelname)-8s | run=%(run_id)-8s | %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"
_COLORS = {"DEBUG": "\033[36m", "INFO": "\033[32m", "WARNING": "\033[33m",
           "ERROR": "\033[31m", "CRITICAL": "\033[1;31m"}

# Files already opened this process. With FILE_MODE="w" the first opener truncates and
# every later one appends, so sibling modules sharing a file don't wipe each other.
_opened: set[str] = set()

# One id per pipeline run, so a failure five modules deep is traceable back to the
# video that caused it. A ContextVar, not a global: Streamlit runs each session's
# script in its own thread and concurrent runs would otherwise overwrite each other.
_run_id: ContextVar[str] = ContextVar("run_id", default="-")


@contextmanager
def run_context(label: str = ""):
    """Tag every log line inside this block with one short id. `label` is logged once."""
    token = _run_id.set(uuid.uuid4().hex[:8])
    logger = get_logger(__name__, "utils.log")   # idempotent; keeps these two lines out of root
    logger.info("run started%s", f": {label}" if label else "")
    try:
        yield _run_id.get()
    finally:
        logger.info("run finished")
        _run_id.reset(token)


class _RunIdFilter(logging.Filter):
    """Supplies %(run_id)s. Without it every un-tagged line raises a formatting error."""

    def filter(self, record):
        record.run_id = _run_id.get()
        return True


class _ColorFormatter(logging.Formatter):
    """Colors the whole console line. Never mutates the record — the file handler sees it too."""

    def format(self, record):
        line = super().format(record)
        color = _COLORS.get(record.levelname)
        return f"{color}{line}\033[0m" if color else line


def get_logger(name: str, log_file: str = None, level=None):
    """
    Returns a logger with a console handler and an optional file handler.

    Args:
        name: Logger name, always __name__.
        log_file: Filename inside LOG_DIR, named after the caller's directory
                  (e.g. "agents.log"). Omit for console-only.
        level: Overrides both CONSOLE_LEVEL and FILE_LEVEL for this logger.
    """
    logger = logging.getLogger(name)

    # Own handlers only — hasHandlers() would find the root's and skip our setup entirely.
    if logger.handlers:
        return logger

    console_level = level or CONSOLE_LEVEL
    file_level = level or FILE_LEVEL
    logger.setLevel(min(console_level, file_level))
    logger.propagate = False        # we own the output; root handlers would double-print
    logger.addFilter(_RunIdFilter())

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(
        _ColorFormatter(_FORMAT, _DATEFMT) if sys.stdout.isatty()   # no escape codes when piped
        else logging.Formatter(_FORMAT, _DATEFMT)
    )
    logger.addHandler(console)

    if log_file:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / log_file
        mode = "a" if str(path) in _opened else FILE_MODE
        _opened.add(str(path))

        file_handler = RotatingFileHandler(path, mode=mode, maxBytes=MAX_BYTES,
                                           backupCount=BACKUP_COUNT, encoding="utf-8")
        file_handler.setLevel(file_level)
        file_handler.setFormatter(logging.Formatter(_FORMAT, _DATEFMT))
        logger.addHandler(file_handler)

    return logger


if __name__ == "__main__":
    # Self-check: python -m src.utils.logging_setup
    import tempfile

    FILE_MODE = "w"
    LOG_DIR = Path(tempfile.mkdtemp())

    a = get_logger("pkg.mod_a", "shared.log")
    b = get_logger("pkg.mod_b", "shared.log")
    a.info("from a")
    b.info("from b")
    with run_context("demo") as rid:
        a.info("inside")

    written = (LOG_DIR / "shared.log").read_text()
    assert "from a" in written, "mode 'w' truncated a sibling module's lines"
    assert "from b" in written
    assert get_logger("pkg.mod_a") is a and len(a.handlers) == 2, "handlers stacked on re-call"
    assert "run=-" in written, "un-tagged lines must still format"
    assert f"run={rid}" in written, "run_context id missing from tagged lines"
    assert _run_id.get() == "-", "run id leaked past its context"
    print("✅ logging_setup self-check passed")
