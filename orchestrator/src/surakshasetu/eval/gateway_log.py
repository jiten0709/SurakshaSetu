"""Bug #43's evaluation gate (decision log §2, "Bug #43 evaluation gate", 2026-11-17).

OmniRoute 3.8.50 learns, per model, the reasoning-effort values an upstream 400 lists, then rewrites
`none` (thinking off, decision 7) on that request's re-send and on later requests to the model. The
adapter sees a 200; the only trace is a structured log event tagged REASONING_SANITIZE. So a run
through omniroute-live fails when its own saved log holds one, and fails closed when the log can't
vouch for the run: missing, empty, a line that doesn't parse, or no request on a route the run's
probe pinged, or when omniroute-live isn't the image this gate was verified on.

The format is `docker compose logs --no-color --timestamps omniroute-live` as `make
gateway-live-down` saves it, over OmniRoute 3.8.50's JSON events. Only parsed fields are read;
message text is never read, logged or reported.
"""

import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

from surakshasetu.eval.metrics import Gate

NAME = "gateway_log:reasoning_sanitize"
SANITIZE = "REASONING_SANITIZE"
# The image whose tag and log format the payload-rules probe (2026-11-17) showed. Before changing
# it, re-run a clamp probe on the new image and recheck SANITIZE, the HTTP event's fields and LINE.
VERIFIED_IMAGE = (
    "diegosouzapw/omniroute:3.8.50"
    "@sha256:085c57adf499a8aaa9f35ccde95c0df9c11bd9ecd18d6c9edbf3b68b8079ba9d"
)
# "<service>-<replica> +| <Docker RFC 3339 timestamp>[ <the container's line>]"
LINE = re.compile(
    r"omniroute-live-\d+ +\| \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)(?: (.*))?"
)
# The run's own traffic: OmniRoute logs one HTTP event per chat request it receives, with path
# "/v1/chat/completions | <combo> | <n> msgs". In the probe, 13 requests gave 13 such events, per
# route as sent; the admin API, the healthcheck and the clamp's re-send gave none.
CHAT = "/v1/chat/completions"


class Scan(NamedTuple):
    sanitized: list[int]  # line numbers of REASONING_SANITIZE events
    requests: Counter[str]  # chat requests received, per combo
    problem: str  # the first reason the log can't vouch for the run; "" when it can


def scan(path: Path) -> Scan:
    """Each line is the compose prefix and timestamp, then text (ignored) or one JSON object. Any
    other line, or a JSON-looking one that doesn't parse, is a problem; the scan goes on, so every
    sanitizer event is still counted."""
    sanitized: list[int] = []
    requests: Counter[str] = Counter()
    try:
        text = path.read_text("utf-8")
    except FileNotFoundError:
        return Scan(sanitized, requests, "missing")
    except (OSError, UnicodeDecodeError) as exc:
        return Scan(sanitized, requests, f"unreadable ({type(exc).__name__})")
    if not text.strip():
        return Scan(sanitized, requests, "empty")
    problem = ""
    # "\n" only: str.splitlines() would also split inside a JSON string at U+2028.
    for number, line in enumerate(text.removesuffix("\n").split("\n"), 1):
        found = LINE.fullmatch(line)
        if found is None:
            problem = problem or f"line {number} is not an omniroute-live log line"
            continue
        body = found[1] or ""
        if not body.startswith("{"):
            continue  # a text line
        try:
            event = json.loads(body)
        except json.JSONDecodeError:
            problem = problem or f"line {number} does not parse as JSON"
            continue
        if event.get("tag") == SANITIZE:
            sanitized.append(number)
        elif event.get("tag") == "HTTP" and event.get("method") == "POST":
            target, _, rest = str(event.get("path", "")).partition(" | ")
            if target == CHAT:
                requests[rest.partition(" | ")[0]] += 1
    return Scan(sanitized, requests, problem)


def gate(path: Path, routes: Sequence[str], image: str | None) -> Gate:
    """The report's gate: the count of sanitizer events, which must be 0. Unmeasured (so FAIL) when
    the log can't vouch for the run: a scan problem, an image other than VERIFIED_IMAGE (`image` is
    omniroute-live's in compose, None when unreadable), no probed routes, or one with no request."""
    found = scan(path)
    missing = [r for r in routes if not found.requests[r]]
    problem = (
        found.problem
        or ("omniroute-live's image could not be read" if image is None else "")
        or ("omniroute-live's image is not VERIFIED_IMAGE" if image != VERIFIED_IMAGE else "")
        or ("the report names no probed routes" if not routes else "")
        or (f"no request on {', '.join(missing)}" if missing else "")
    )
    note = f"{path.name}: chat requests " + (
        ", ".join(f"{r} {found.requests[r]}" for r in routes) or "none expected"
    )
    if hits := found.sanitized:
        note += f"; {len(hits)} {SANITIZE} events, the first on line {hits[0]}"
    if problem:
        note += f"; fails closed: {problem}"
    value = None if problem else float(len(found.sanitized))
    return Gate(NAME, "gateway_log", value, "==", 0, True, note + ".")
