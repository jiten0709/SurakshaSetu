"""The nightly anchor (TDD §4.3): verify every chain active on a day, then anchor the day's Merkle
root in object-locked storage and in audit.chain_anchor.

Anchor closed days. An anchor row is insert-only, so an anchor taken while its day was still open
can never cover that day's later events.
"""

import hashlib
import io
import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from minio import Minio
from minio.commonconfig import COMPLIANCE
from minio.retention import Retention

from surakshasetu.audit.chain import Conn, verify_session
from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import canonical_json

logger = logging.getLogger(__name__)

BUCKET = "audit-anchors"
DEV_TSA_SEED = b"surakshasetu-dev-tsa"


class ChainBroken(Exception):
    """A chain failed verification, or an anchored day's root changed. Already logged CRITICAL."""


@dataclass(frozen=True)
class Anchor:
    root: bytes
    sessions: int
    events: int
    worm_key: str | None  # None when the day was already anchored


def leaf(session_id: UUID, last_hash: bytes) -> bytes:
    return hashlib.sha256(session_id.bytes + last_hash).digest()


def merkle_root(leaves: list[bytes]) -> bytes:
    """Binary SHA-256 tree; an odd node is promoted unchanged. An empty day is SHA-256(b"")."""
    level = leaves or [hashlib.sha256(b"").digest()]
    while len(level) > 1:
        level = [
            hashlib.sha256(level[i] + level[i + 1]).digest() if i + 1 < len(level) else level[i]
            for i in range(0, len(level), 2)
        ]
    return level[0]


def anchor_day(
    conn: Conn, day: date, *, s3: Minio, tsa_key: Ed25519PrivateKey, retention_days: int
) -> Anchor:
    """Runs in the caller's transaction. Raises ChainBroken without writing anything."""
    start = datetime.combine(day, time(), UTC)
    end = start + timedelta(days=1)
    # Each leaf takes the session's last hash before the day ends, so a past day's root stays
    # reproducible for sessions that carry on past midnight.
    rows = conn.execute(
        "WITH active AS (SELECT DISTINCT session_id FROM audit.audit_event"
        "  WHERE occurred_at >= %s AND occurred_at < %s)"
        " SELECT e.session_id, count(*) FILTER (WHERE e.occurred_at >= %s),"
        "  (array_agg(e.hash ORDER BY e.seq DESC))[1]"
        " FROM audit.audit_event e JOIN active USING (session_id)"
        " WHERE e.occurred_at < %s GROUP BY e.session_id",
        (start, end, start, end),
    ).fetchall()
    rows.sort(key=lambda row: row[0].bytes)
    broken = sum(not verify_session(conn, session_id).ok for session_id, _, _ in rows)
    if broken:
        raise ChainBroken(f"{broken} broken chain(s) active on {day}")

    root = merkle_root([leaf(session_id, bytes(last)) for session_id, _, last in rows])
    sessions, events = len(rows), sum(count for _, count, _ in rows)
    existing = conn.execute(
        "SELECT merkle_root, created_at FROM audit.chain_anchor WHERE anchor_date = %s", (day,)
    ).fetchone()
    if existing:
        if bytes(existing[0]) == root:
            logger.info("%s already anchored; root unchanged", day)
        elif existing[1] >= end:
            logger.critical("AUDIT_CHAIN_BROKEN the anchored root for %s changed", day)
            raise ChainBroken(f"the anchored root for {day} changed")
        else:
            logger.warning("%s was anchored while open; its later events are not anchored", day)
        return Anchor(root=root, sessions=sessions, events=events, worm_key=None)

    created_at = datetime.now(UTC)
    body = canonical_json(
        {
            "root": root.hex(),
            "sessions": sessions,
            "events": events,
            "created_at": created_at.isoformat(),
        }
    )
    name = f"{day.isoformat()}.json"
    written = s3.put_object(
        BUCKET,
        name,
        io.BytesIO(body),
        len(body),
        content_type="application/json",
        retention=Retention(COMPLIANCE, created_at + timedelta(days=retention_days)),
    )
    # Both local databases share the bucket, so the version id names the exact object.
    worm_key = f"{BUCKET}/{name}?versionId={written.version_id}"
    tsa_token = tsa_key.sign(root + day.isoformat().encode())
    conn.execute(
        "INSERT INTO audit.chain_anchor"
        " (anchor_date, merkle_root, sessions, events, worm_key, tsa_token, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (day, root, sessions, events, worm_key, tsa_token, created_at),
    )
    logger.info("anchored %s: sessions=%d events=%d root=%s", day, sessions, events, root.hex())
    return Anchor(root=root, sessions=sessions, events=events, worm_key=worm_key)


def load_tsa_key(path: Path | None) -> Ed25519PrivateKey:
    if path is None:
        # Dev and test only (Settings requires SS_TSA_KEY_PATH in pilot and prod): anyone can
        # derive this key, so its tokens prove nothing.
        return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(DEV_TSA_SEED).digest())
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("SS_TSA_KEY_PATH must hold an Ed25519 private key")
    return key


def minio_client(settings: Settings) -> Minio:
    url = urlsplit(settings.minio_endpoint)
    return Minio(
        url.netloc,
        access_key=settings.minio_access_key.get_secret_value(),
        secret_key=settings.minio_secret_key.get_secret_value(),
        secure=url.scheme == "https",
    )
