"""The per-session audit hash chain in audit.audit_event (TDD §4.3).

hash = SHA-256(prev_hash || JCS(hashed) || SHA-256(payload_enc)), where `hashed` holds every
non-personal column: event_id, session_id, seq, event_type, occurred_at, fsm_state, pins, key_ref
and header. The TDD writes the middle term as the header alone; widening it (decided 2026-09-25)
makes an edit of fsm_state or pins, or a backdated occurred_at, detectable. The payload enters
only through its ciphertext's hash, so verification needs no key and survives crypto-shredding.
The genesis prev_hash is 32 zero bytes.

Nothing here commits, updates or deletes. append runs in the caller's transaction, so the audit
rows commit together with the conv rows, before the response is released (I8).
"""

import dataclasses
import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.pq import TransactionStatus
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from surakshasetu.audit.events import HEADERS, EventType, Header
from surakshasetu.crypto import envelope
from surakshasetu.crypto.jcs import canonical_json
from surakshasetu.crypto.keys import SYSTEM_KEY_REF, KeyService
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger(__name__)

GENESIS = bytes(32)
SYSTEM_SESSION = UUID(int=0)  # the one chain for events that belong to no session
SYSTEM_EVENTS = frozenset({EventType.CONFIG_RELEASE, EventType.KILL_SWITCH})

Conn = psycopg.Connection[Any]


@dataclasses.dataclass(frozen=True)
class AuditEvent:
    event_id: UUID
    session_id: UUID
    seq: int
    event_type: str
    occurred_at: datetime
    fsm_state: str
    pins: dict[str, Any]
    header: dict[str, Any]
    payload_enc: bytes
    key_ref: str
    prev_hash: bytes
    hash: bytes


@dataclasses.dataclass(frozen=True)
class VerifyResult:
    ok: bool
    checked: int
    first_bad_seq: int | None = None
    gap_at: int | None = None


def append(
    conn: Conn,
    keys: KeyService,
    *,
    session_id: UUID,
    event_type: EventType,
    fsm_state: str,
    pins: Mapping[str, Any],
    header: Header,
    payload: Mapping[str, Any],
    key_ref: str,
) -> AuditEvent:
    """Append one event to the session's chain, inside the caller's transaction.

    The payload must be JSON-ready (run models through model_dump(mode="json") first). Needs READ
    COMMITTED, the default: after the lock, the SELECT must see the previous holder's committed
    row. Appends to one session serialise on the advisory lock; other sessions don't wait.
    """
    if type(header) is not HEADERS[event_type]:
        raise TypeError(f"{event_type} takes a {HEADERS[event_type].__name__}")
    is_system = session_id == SYSTEM_SESSION
    if (event_type in SYSTEM_EVENTS) != is_system or is_system != (key_ref == SYSTEM_KEY_REF):
        raise ValueError("CONFIG_RELEASE and KILL_SWITCH, and only they, use the system chain")
    if conn.autocommit and conn.info.transaction_status != TransactionStatus.INTRANS:
        raise RuntimeError("append needs the caller's transaction; the lock would not hold")

    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))", (session_id,))
    last = conn.execute(
        "SELECT seq, hash FROM audit.audit_event WHERE session_id = %s ORDER BY seq DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    seq, prev_hash = (last[0] + 1, bytes(last[1])) if last else (1, GENESIS)
    unhashed = AuditEvent(
        event_id=uuid7(),
        session_id=session_id,
        seq=seq,
        event_type=event_type.value,
        occurred_at=datetime.now(UTC),  # the server clock, NTP-synced in prod
        fsm_state=fsm_state,
        pins=dict(pins),
        header=header.model_dump(mode="json"),
        payload_enc=envelope.encrypt(
            keys.dek(key_ref), canonical_json(dict(payload)), _aad(session_id, seq)
        ),
        key_ref=key_ref,
        prev_hash=prev_hash,
        hash=b"",
    )
    event = dataclasses.replace(unhashed, hash=chain_hash(unhashed))
    conn.execute(
        "INSERT INTO audit.audit_event (event_id, session_id, seq, event_type, occurred_at,"
        " fsm_state, pins, header, payload_enc, key_ref, prev_hash, hash)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            event.event_id,
            event.session_id,
            event.seq,
            event.event_type,
            event.occurred_at,
            event.fsm_state,
            Jsonb(event.pins),
            Jsonb(event.header),
            event.payload_enc,
            event.key_ref,
            event.prev_hash,
            event.hash,
        ),
    )
    logger.debug("audit append seq=%d type=%s", seq, event_type)
    return event


def chain_hash(event: AuditEvent) -> bytes:
    hashed = {
        "event_id": str(event.event_id),
        "session_id": str(event.session_id),
        "seq": event.seq,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
        "fsm_state": event.fsm_state,
        "pins": event.pins,
        "key_ref": event.key_ref,
        "header": event.header,
    }
    payload_hash = hashlib.sha256(event.payload_enc).digest()
    return hashlib.sha256(event.prev_hash + canonical_json(hashed) + payload_hash).digest()


def events(conn: Conn, session_id: UUID) -> list[AuditEvent]:
    with conn.cursor(row_factory=class_row(AuditEvent)) as cur:
        return cur.execute(
            "SELECT event_id, session_id, seq, event_type, occurred_at, fsm_state, pins, header,"
            " payload_enc, key_ref, prev_hash, hash"
            " FROM audit.audit_event WHERE session_id = %s ORDER BY seq",
            (session_id,),
        ).fetchall()


def verify_session(conn: Conn, session_id: UUID) -> VerifyResult:
    """Recompute the chain from genesis. Needs no key: hashes cover ciphertext, not plaintext."""
    prev, checked = GENESIS, 0
    for event in events(conn, session_id):
        if event.seq != checked + 1:
            return _broken(session_id, VerifyResult(ok=False, checked=checked, gap_at=checked + 1))
        if event.prev_hash != prev or chain_hash(event) != event.hash:
            return _broken(
                session_id, VerifyResult(ok=False, checked=checked, first_bad_seq=event.seq)
            )
        prev, checked = event.hash, checked + 1
    return VerifyResult(ok=True, checked=checked)


def _broken(session_id: UUID, result: VerifyResult) -> VerifyResult:
    # Pages security in prod. Logged here, so every caller that finds a break pages.
    logger.critical(
        "AUDIT_CHAIN_BROKEN session=%s first_bad_seq=%s gap_at=%s",
        session_id,
        result.first_bad_seq,
        result.gap_at,
    )
    return result


def decrypt_payload(keys: KeyService, event: AuditEvent) -> Any:
    """Raises KeyDestroyed once the subject's key is shredded: a normal outcome, shown as
    "[erased]"."""
    blob = envelope.decrypt(
        keys.dek(event.key_ref), event.payload_enc, _aad(event.session_id, event.seq)
    )
    return json.loads(blob)


def _aad(session_id: UUID, seq: int) -> str:
    return f"audit:{session_id}:{seq}"
