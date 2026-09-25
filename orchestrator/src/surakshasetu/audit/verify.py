"""Nightly audit verification: `python -m surakshasetu.audit.verify --date YYYY-MM-DD`.

Verifies every chain with events on that day, then anchors the day's Merkle root. Exits 1 when a
chain is broken or an anchored root changed; both are logged CRITICAL AUDIT_CHAIN_BROKEN, which
pages security in prod. Connects as app_rw (SS_PG_DSN_APP), which may INSERT into chain_anchor.
"""

import argparse
import logging
import sys
from datetime import UTC, date, datetime

import psycopg

from surakshasetu.audit.anchor import ChainBroken, anchor_day, load_tsa_key, minio_client
from surakshasetu.config import ConfigError, load_settings
from surakshasetu.logging import configure_logging

# Explicit name: run with -m, __name__ is "__main__" and would land in lib.log.
logger = logging.getLogger("surakshasetu.audit.verify")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m surakshasetu.audit.verify")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="YYYY-MM-DD, UTC")
    day: date = parser.parse_args(argv).date
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    if settings.pg_dsn_app is None:
        raise ConfigError("SS_PG_DSN_APP is required")
    if settings.env in ("pilot", "prod") and day >= datetime.now(UTC).date():
        parser.error(f"{day} is still open; anchor closed days only")

    with psycopg.connect(settings.pg_dsn_app.get_secret_value()) as conn:
        try:
            anchor_day(
                conn,
                day,
                s3=minio_client(settings),
                tsa_key=load_tsa_key(settings.tsa_key_path),
                retention_days=settings.anchor_retention_days,
            )
        except ChainBroken:
            return 1
    logger.info("audit verified for %s", day)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as exc:
        sys.exit(str(exc))
