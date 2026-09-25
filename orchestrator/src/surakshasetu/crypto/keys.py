"""Per-subject data-encryption keys (DEKs).

Erasure is crypto-shredding: destroying a subject's key makes every ciphertext under it unreadable,
while the rows that hold it, and the audit chain over them, stay intact and verifiable.
"""

import hashlib
import logging
import os
import time
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

from surakshasetu.crypto import envelope
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger(__name__)

# Encrypts the payloads of the system chain (CONFIG_RELEASE, KILL_SWITCH). Never destroyed.
SYSTEM_KEY_REF = "system"
DEK_CACHE_SECONDS = 60.0


class KeyDestroyed(Exception):
    """The key was crypto-shredded. A normal outcome: callers render the payload as "[erased]"."""


class KeyService(Protocol):
    def create_subject_key(self, subject_ref: UUID) -> str: ...
    def dek(self, key_ref: str) -> bytes: ...
    def schedule_destruction(self, key_ref: str, after: datetime) -> None: ...
    def destroy(self, key_ref: str) -> None: ...
    def destroy_due(self, now: datetime) -> int: ...


class LocalKeyService:
    """DEKs wrapped with AES-256-GCM under a KEK from SS_KEK_B64 (AAD = key_ref), stored in
    keyvault.subject_key through a keyvault_rw pool. Production puts an India-region KMS/HSM
    behind the same protocol."""

    def __init__(self, pool: ConnectionPool[psycopg.Connection[TupleRow]], kek: bytes) -> None:
        if len(kek) != 32:
            raise ValueError("the KEK must be 32 bytes (AES-256)")
        self._pool = pool
        self._kek = kek
        self.kek_id = "local-" + hashlib.sha256(kek).hexdigest()[:16]
        self._cache: dict[str, tuple[bytes, float]] = {}

    def create_subject_key(self, subject_ref: UUID) -> str:
        key_ref = str(uuid7())
        self._insert(key_ref, subject_ref)
        return key_ref

    def dek(self, key_ref: str) -> bytes:
        cached = self._cache.get(key_ref)
        if cached and cached[1] > time.monotonic():
            return cached[0]
        row = self._fetch(key_ref)
        if row is None and key_ref == SYSTEM_KEY_REF:
            self._insert(SYSTEM_KEY_REF, UUID(int=0))
            row = self._fetch(key_ref)
        if row is None:
            raise LookupError("unknown key_ref")
        wrapped, kek_id = row
        if wrapped is None:
            raise KeyDestroyed
        if kek_id != self.kek_id:
            raise LookupError(f"key is wrapped under {kek_id}; this service holds {self.kek_id}")
        dek = envelope.decrypt(self._kek, wrapped, key_ref)
        self._cache[key_ref] = (dek, time.monotonic() + DEK_CACHE_SECONDS)
        return dek

    def schedule_destruction(self, key_ref: str, after: datetime) -> None:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE keyvault.subject_key SET destroy_after = %s WHERE key_ref = %s",
                (after, key_ref),
            )
        if cur.rowcount == 0:
            raise LookupError("unknown key_ref")

    def destroy(self, key_ref: str) -> None:
        if key_ref == SYSTEM_KEY_REF:
            raise ValueError("the system key is never destroyed")
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE keyvault.subject_key SET wrapped_dek = NULL,"
                " destroyed_at = coalesce(destroyed_at, now()) WHERE key_ref = %s",
                (key_ref,),
            )
        if cur.rowcount == 0:
            raise LookupError("unknown key_ref")
        self._cache.pop(key_ref, None)
        logger.info("subject key destroyed")

    def destroy_due(self, now: datetime) -> int:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "UPDATE keyvault.subject_key SET wrapped_dek = NULL, destroyed_at = now()"
                " WHERE destroy_after <= %s AND destroyed_at IS NULL AND key_ref <> %s"
                " RETURNING key_ref",
                (now, SYSTEM_KEY_REF),
            ).fetchall()
        for (key_ref,) in rows:
            self._cache.pop(key_ref, None)
        logger.info("destroyed %d due subject keys", len(rows))
        return len(rows)

    def _fetch(self, key_ref: str) -> tuple[Any, ...] | None:
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT wrapped_dek, kek_id FROM keyvault.subject_key WHERE key_ref = %s",
                (key_ref,),
            ).fetchone()

    def _insert(self, key_ref: str, subject_ref: UUID) -> None:
        wrapped = envelope.encrypt(self._kek, os.urandom(32), key_ref)
        with self._pool.connection() as conn:
            conn.execute(
                "INSERT INTO keyvault.subject_key (key_ref, subject_ref, wrapped_dek, kek_id)"
                " VALUES (%s, %s, %s, %s) ON CONFLICT (key_ref) DO NOTHING",
                (key_ref, subject_ref, wrapped, self.kek_id),
            )
