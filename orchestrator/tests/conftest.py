"""Shared fixtures. The database ones read test DSNs that only `make check-db` and
`make check-stack` set, so the default suite never touches a database."""

import base64
import logging
import os
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from surakshasetu.config import Settings
from surakshasetu.crypto.keys import LocalKeyService

# tests/ingest needs the ingest dependency group (docling, dagster, qdrant-client), which check-py
# and CI never install: only `make check-ingest` (SS_TEST_INGEST=1) collects it.
collect_ignore = [] if os.environ.get("SS_TEST_INGEST") else ["ingest"]


@pytest.fixture
def restore_logging() -> Iterator[None]:
    """Undo configure_logging, so a test's handlers and levels don't leak into the next."""
    root = logging.getLogger()
    ours = logging.getLogger("surakshasetu")
    saved = (root.handlers[:], root.level, ours.level)
    yield
    for handler in root.handlers:
        if handler not in saved[0]:
            handler.close()
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    ours.setLevel(saved[2])


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is not set; run `make up && make check-db` (or `make check-stack`)")
    return value


@pytest.fixture
def admin_dsn() -> str:
    return _env("SS_TEST_PG_DSN_ADMIN")


@pytest.fixture
def db(admin_dsn: str) -> Iterator[psycopg.Connection[tuple[Any, ...]]]:
    # A superuser session can SET ROLE to any role; force_rollback discards every write.
    with psycopg.connect(admin_dsn, autocommit=True) as conn, conn.transaction(force_rollback=True):
        yield conn


@pytest.fixture
def keys() -> Iterator[LocalKeyService]:
    # Keys commit in surakshasetu_test, including the system key, so every run must wrap and
    # unwrap under the same KEK: the dev default.
    kek = base64.b64decode(Settings(_env_file=None).kek_b64.get_secret_value())
    with ConnectionPool(_env("SS_TEST_PG_DSN_KEYVAULT"), min_size=1) as pool:
        yield LocalKeyService(pool, kek)


@pytest.fixture
def system_chain_tail(admin_dsn: str) -> Iterator[None]:
    """For tests that commit on the system chain (a runtime's CONFIG_RELEASE, a KILL_SWITCH): the
    events they add are removed afterwards, tail only, so the chain still verifies and the db
    tests see the test database's system chain as they left it."""
    system = "00000000-0000-0000-0000-000000000000"
    query = "SELECT coalesce(max(seq), 0) FROM audit.audit_event WHERE session_id = %s"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        row = conn.execute(query, (system,)).fetchone()
    before = row[0] if row else 0
    yield
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "DELETE FROM audit.audit_event WHERE session_id = %s AND seq > %s", (system, before)
        )
