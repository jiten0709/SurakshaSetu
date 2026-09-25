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
