"""Inactivity timers: TDD §3.9's row CC3 (Step 22).

Every SS_TIMER_INTERVAL_S seconds (`python -m surakshasetu.jobs.timers --every 60`; once with
--once, which `make timers-once` runs) the job looks for active sessions with no turn for longer
than their state's timeout (SS_INACTIVITY_TIMEOUTS) and runs a timer turn for each
(Runtime.run_timer): CC3 pauses the session, and the pause is committed and audited like any turn.
Its line goes out on the session's event stream, to a client that still listens; nothing else is
sent.

Before consent a session is passive (V3): it is never selected, so nothing transitions and nothing
goes out. Re-engagement messages are phase 2 (TDD §3.9 allows them only within the granted
purposes). A customer turn resumes a paused session (PAUSE.R), which revalidates the pins, the
products and quotes, the notice version and the consent TTL (graph/handlers/pause.resume).

The compose `scheduler` service (profile app, `make app-up`; Step 25) runs it in the orchestrator
image with `--every $SS_TIMER_INTERVAL_S --heartbeat /tmp/timers.heartbeat`: the file is touched
after each look, and the container's healthcheck reads its age.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from surakshasetu.config import ConfigError, load_settings
from surakshasetu.graph.runtime import ProblemError, Runtime
from surakshasetu.logging import configure_logging
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

logger = logging.getLogger("surakshasetu.jobs.timers")


def due(rows: Iterable[SessionRow], now: datetime, timeouts: Mapping[str, int]) -> list[SessionRow]:
    """The sessions a timer pauses now: active, after consent (V3), in a state with a timeout, and
    idle at least that long."""
    return [
        r
        for r in rows
        if r.status == "active"
        and r.consent_id is not None
        and r.fsm_state in timeouts
        and r.last_activity_at + timedelta(seconds=timeouts[r.fsm_state]) <= now
    ]


async def run_once(
    runtime: Runtime, now: datetime, session_ids: list[UUID] | None = None
) -> list[bytes]:
    """One look: a timer turn for every session due. A busy session (a customer turn holds the
    lock) waits for the next look; any other failure is logged and the next session runs. Returns
    the released bodies."""
    timeouts = runtime.settings.inactivity_timeouts
    if not timeouts:
        return []
    before = now - timedelta(seconds=min(timeouts.values()))
    async with runtime.connection() as conn:
        rows = store.inactive_sessions(conn, before, session_ids)
    released: list[bytes] = []
    for row in due(rows, now, timeouts):
        try:
            body = await runtime.run_timer(row)
        except ProblemError as exc:
            logger.info("timer skipped a session: %s", exc.code)
            continue
        except Exception:
            logger.exception("timer turn failed; the next look retries it")
            continue
        if body is not None:
            released.append(body)
    logger.info("timers: %d idle, %d paused", len(rows), len(released))
    return released


async def _loop(once: bool, every: int | None, heartbeat: Path | None = None) -> int:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    interval = every or settings.timer_interval_s
    if heartbeat is not None:  # a restarted container keeps the last process's heartbeat
        await asyncio.to_thread(heartbeat.unlink, missing_ok=True)
    async with Runtime.open(settings) as runtime:
        while True:
            await run_once(runtime, datetime.now(UTC))
            if heartbeat is not None:
                await asyncio.to_thread(heartbeat.touch)
            if once:
                return 0
            await asyncio.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m surakshasetu.jobs.timers")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one look, then exit")
    mode.add_argument("--every", type=int, metavar="SECONDS", help="look every SECONDS")
    parser.add_argument(
        "--heartbeat", type=Path, metavar="FILE", help="touched after each look (healthcheck)"
    )
    args = parser.parse_args(argv)
    return asyncio.run(_loop(args.once, args.every, args.heartbeat))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as exc:
        sys.exit(str(exc))
