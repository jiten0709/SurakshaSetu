"""Create the LangGraph checkpointer's tables in the langgraph schema.

Run as `python -m surakshasetu.graph.checkpointer_setup` after Flyway has created the empty schema.
SS_PG_DSN_APP must be the app_rw DSN: app_rw then owns the tables, and search_path is pinned so
they land in langgraph rather than public.
"""

import logging
import sys

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

from surakshasetu.config import ConfigError, load_settings
from surakshasetu.logging import configure_logging

# Explicit name: run with -m, __name__ is "__main__" and would land in lib.log.
logger = logging.getLogger("surakshasetu.graph.checkpointer_setup")


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    if settings.pg_dsn_app is None:
        raise ConfigError("SS_PG_DSN_APP is required")
    # PostgresSaver needs autocommit (its indexes are CREATE INDEX CONCURRENTLY) and dict rows.
    with psycopg.connect(
        settings.pg_dsn_app.get_secret_value(),
        options="-c search_path=langgraph",
        autocommit=True,
        row_factory=dict_row,
    ) as conn:
        logger.info("creating checkpointer tables in schema langgraph")
        PostgresSaver(conn).setup()
    logger.info("checkpointer tables ready")


if __name__ == "__main__":
    try:
        main()
    except ConfigError as exc:
        sys.exit(str(exc))
