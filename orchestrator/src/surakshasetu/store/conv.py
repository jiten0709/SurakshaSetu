"""SQL for the conv schema, as app_rw (TDD §7.2). Nothing here commits: the caller's transaction
holds the turn's conv rows and its audit events together, and commits them before release (I8).

Personal values are encrypted under the subject DEK with the Step 4 AADs: "conv.turn:<turn_id>",
"conv.slot_value:<session_id>:<slot>", and (new here) "conv.handoff:<handoff_id>". Ids are UUIDv7
from the application; slot_value_id is the table's bigserial.

Two columns deliberately hold no customer words (decided 2026-10-02): conv.turn.analysis is a
non-personal projection of TurnAnalysis (intents, language, slot names and confidences), and
conv.slot_value.evidence stays NULL. The words live only in the encrypted turn text and the
TURN_INPUT payload.
"""

import dataclasses
import json
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import class_row
from psycopg.types.json import Jsonb

from surakshasetu.analysis.models import TurnAnalysis
from surakshasetu.audit.chain import AuditEvent, decrypt_payload
from surakshasetu.audit.events import EventType
from surakshasetu.crypto import envelope
from surakshasetu.crypto.jcs import canonical_json
from surakshasetu.crypto.keys import KeyService
from surakshasetu.graph.state import DisclosureAck, RecommendationPayload
from surakshasetu.uuid7 import uuid7

Conn = psycopg.Connection[Any]


@dataclasses.dataclass(frozen=True)
class SessionRow:
    session_id: UUID
    subject_ref: UUID
    key_ref: str
    channel: str
    locale: str
    fsm_state: str
    frame_stack: list[dict[str, Any]]
    pins: dict[str, Any]
    status: str
    created_at: datetime
    last_activity_at: datetime
    expires_at: datetime
    token_sha256: bytes
    consent_id: UUID | None
    counters: dict[str, int]


_SELECT_SESSION = (
    "SELECT session_id, subject_ref, key_ref, channel, locale, fsm_state, frame_stack, pins,"
    " status, created_at, last_activity_at, expires_at, token_sha256, consent_id, counters"
    " FROM conv.session WHERE session_id = %s"
)


def insert_session(
    conn: Conn,
    *,
    session_id: UUID,
    subject_ref: UUID,
    key_ref: str,
    channel: str,
    locale: str,
    pins: dict[str, Any],
    expires_at: datetime,
    token_sha256: bytes,
) -> None:
    conn.execute(
        "INSERT INTO conv.session (session_id, subject_ref, key_ref, channel, locale, fsm_state,"
        " pins, expires_at, token_sha256) VALUES (%s, %s, %s, %s, %s, 'S0', %s, %s, %s)",
        (session_id, subject_ref, key_ref, channel, locale, Jsonb(pins), expires_at, token_sha256),
    )


def get_session(conn: Conn, session_id: UUID, *, lock: bool = False) -> SessionRow | None:
    """lock=True takes the row lock for the turn's transaction, NOWAIT: a second writer (only
    possible if the Redis lock lapsed) fails with LockNotAvailable instead of blocking the loop."""
    sql = _SELECT_SESSION + (" FOR UPDATE NOWAIT" if lock else "")
    with conn.cursor(row_factory=class_row(SessionRow)) as cur:
        row = cur.execute(sql, (session_id,)).fetchone()
    return None if row is None else dataclasses.replace(row, token_sha256=bytes(row.token_sha256))


def update_session(
    conn: Conn,
    session_id: UUID,
    *,
    fsm_state: str,
    frame_stack: list[dict[str, Any]],
    counters: dict[str, int],
    pins: dict[str, Any],
    locale: str,
    status: str,
    consent_id: UUID | None,
) -> None:
    conn.execute(
        "UPDATE conv.session SET fsm_state = %s, frame_stack = %s, counters = %s, pins = %s,"
        " locale = %s, status = %s, consent_id = %s, last_activity_at = now()"
        " WHERE session_id = %s",
        (
            fsm_state,
            Jsonb(frame_stack),
            Jsonb(counters),
            Jsonb(pins),
            locale,
            status,
            consent_id,
            session_id,
        ),
    )


def last_seq(conn: Conn, session_id: UUID) -> int:
    row = conn.execute(
        "SELECT coalesce(max(seq), 0) FROM conv.turn WHERE session_id = %s", (session_id,)
    ).fetchone()
    return int(row[0]) if row else 0


def analysis_projection(analysis: TurnAnalysis | None) -> dict[str, Any] | None:
    """What conv.turn.analysis keeps: no slot values, evidence spans or side-query text."""
    if analysis is None:
        return None
    return {
        "intents": [i.value for i in analysis.intents],
        "language": analysis.language,
        "slots": [{"slot": s.slot, "confidence": s.confidence} for s in analysis.slots],
        "has_side_query": analysis.side_query is not None,
    }


def insert_turn(
    conn: Conn,
    keys: KeyService,
    key_ref: str,
    *,
    turn_id: UUID,
    session_id: UUID,
    seq: int,
    direction: str,
    text: str,
    redacted: str,
    language: str,
    analysis: dict[str, Any] | None,
    turn_key: UUID,
) -> None:
    text_enc = envelope.encrypt(keys.dek(key_ref), text.encode(), f"conv.turn:{turn_id}")
    conn.execute(
        "INSERT INTO conv.turn (turn_id, session_id, seq, direction, text_enc, redacted, language,"
        " analysis, turn_key) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            turn_id,
            session_id,
            seq,
            direction,
            text_enc,
            redacted,
            language,
            None if analysis is None else Jsonb(analysis),
            turn_key,
        ),
    )


def turn_text(conn: Conn, keys: KeyService, key_ref: str, turn_id: UUID) -> str:
    row = conn.execute("SELECT text_enc FROM conv.turn WHERE turn_id = %s", (turn_id,)).fetchone()
    if row is None:
        raise LookupError("no such turn")
    return envelope.decrypt(keys.dek(key_ref), bytes(row[0]), f"conv.turn:{turn_id}").decode()


def released_response(
    conn: Conn, keys: KeyService, session_id: UUID, turn_key: UUID
) -> dict[str, Any] | None:
    """The response released for this Idempotency-Key, from its RESPONSE_RELEASED payload (the
    legal record of what left), or None if no turn with this key was committed."""
    found = conn.execute(
        "SELECT o.turn_id FROM conv.turn i JOIN conv.turn o"
        " ON o.session_id = i.session_id AND o.seq = i.seq + 1 AND o.direction = 'out'"
        " WHERE i.session_id = %s AND i.turn_key = %s AND i.direction = 'in'",
        (session_id, turn_key),
    ).fetchone()
    if found is None:
        return None
    with conn.cursor(row_factory=class_row(AuditEvent)) as cur:
        event = cur.execute(
            "SELECT event_id, session_id, seq, event_type, occurred_at, fsm_state, pins, header,"
            " payload_enc, key_ref, prev_hash, hash FROM audit.audit_event"
            " WHERE session_id = %s AND event_type = %s AND header->>'turn_id' = %s",
            (session_id, EventType.RESPONSE_RELEASED.value, str(found[0])),
        ).fetchone()
    if event is None:
        raise LookupError("a committed turn without its RESPONSE_RELEASED event")
    response: dict[str, Any] = decrypt_payload(keys, event)["response"]
    return response


def insert_slot(
    conn: Conn,
    keys: KeyService,
    key_ref: str,
    *,
    session_id: UUID,
    slot: str,
    value: Any,
    confidence: float,
    status: str,
    source_turn: UUID | None,
    consent_id: UUID,
) -> int:
    """Append one slot row (never updated: a correction is a new row). consent_id is NOT NULL and
    a foreign key to consent.record, the database's I1 backstop."""
    value_enc = envelope.encrypt(
        keys.dek(key_ref), canonical_json(value), f"conv.slot_value:{session_id}:{slot}"
    )
    row = conn.execute(
        "INSERT INTO conv.slot_value (session_id, slot, value_enc, confidence, status,"
        " source_turn, consent_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING slot_value_id",
        (session_id, slot, value_enc, confidence, status, source_turn, consent_id),
    ).fetchone()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING returned no row")
    return int(row[0])


def current_slots(conn: Conn, keys: KeyService, key_ref: str, session_id: UUID) -> dict[str, Any]:
    """slot -> value of the newest confirmed row per slot (TDD §7.2). Rows of one transaction share
    created_at, so slot_value_id breaks the tie."""
    rows = conn.execute(
        "SELECT DISTINCT ON (slot) slot, value_enc FROM conv.slot_value"
        " WHERE session_id = %s AND status = 'confirmed'"
        " ORDER BY slot, created_at DESC, slot_value_id DESC",
        (session_id,),
    ).fetchall()
    dek = keys.dek(key_ref)
    return {
        slot: json.loads(envelope.decrypt(dek, bytes(enc), f"conv.slot_value:{session_id}:{slot}"))
        for slot, enc in rows
    }


def latest_slots(
    conn: Conn, keys: KeyService, key_ref: str, session_id: UUID
) -> dict[str, tuple[str, Any]]:
    """slot -> (status, value) of the newest row per slot, whatever its status (Step 19): what S1
    and Quote-Only have collected so far, confirmed or not."""
    rows = conn.execute(
        "SELECT DISTINCT ON (slot) slot, status, value_enc FROM conv.slot_value"
        " WHERE session_id = %s ORDER BY slot, created_at DESC, slot_value_id DESC",
        (session_id,),
    ).fetchall()
    return {
        slot: (
            status,
            json.loads(
                envelope.decrypt(
                    keys.dek(key_ref), bytes(enc), f"conv.slot_value:{session_id}:{slot}"
                )
            ),
        )
        for slot, status, enc in rows
    }


def insert_recommendation(conn: Conn, *, session_id: UUID, payload: RecommendationPayload) -> UUID:
    rec_id = uuid7()
    conn.execute(
        "INSERT INTO conv.recommendation (rec_id, session_id, options, ranker_version,"
        " suitability_inputs_sha256, rendered_sha256) VALUES (%s, %s, %s, %s, %s, %s)",
        (
            rec_id,
            session_id,
            Jsonb([o.model_dump(mode="json") for o in payload.options]),
            payload.ranker_version,
            bytes.fromhex(payload.suitability_inputs_sha256),
            bytes.fromhex(payload.rendered_sha256),
        ),
    )
    return rec_id


def insert_disclosure_ack(conn: Conn, *, rec_id: UUID, ack: DisclosureAck) -> UUID:
    ack_id = uuid7()
    conn.execute(
        "INSERT INTO conv.disclosure_ack (ack_id, rec_id, uin, registry_version, set_sha256,"
        " document_sha256, acked_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (
            ack_id,
            rec_id,
            ack.uin,
            ack.registry_version,
            bytes.fromhex(ack.disclosure_set_sha256),
            Jsonb(ack.document_sha256),
            ack.acked_at,
        ),
    )
    return ack_id


def insert_handoff(
    conn: Conn,
    keys: KeyService,
    key_ref: str,
    *,
    session_id: UUID,
    reason_code: str,
    queue: str,
    payload: dict[str, Any],
) -> UUID:
    handoff_id = uuid7()
    payload_enc = envelope.encrypt(
        keys.dek(key_ref), canonical_json(payload), f"conv.handoff:{handoff_id}"
    )
    conn.execute(
        "INSERT INTO conv.handoff (handoff_id, session_id, reason_code, queue, payload_enc)"
        " VALUES (%s, %s, %s, %s, %s)",
        (handoff_id, session_id, reason_code, queue, payload_enc),
    )
    return handoff_id


@dataclasses.dataclass(frozen=True)
class HandoffRow:
    handoff_id: UUID
    session_id: UUID
    reason_code: str
    queue: str
    created_at: datetime
    picked_at: datetime | None


def has_handoff(conn: Conn, session_id: UUID) -> bool:
    row = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM conv.handoff WHERE session_id = %s)", (session_id,)
    ).fetchone()
    return bool(row and row[0])


def list_handoffs(conn: Conn, queue: str) -> list[HandoffRow]:
    """The queue, oldest first. Metadata only: the briefing stays encrypted."""
    with conn.cursor(row_factory=class_row(HandoffRow)) as cur:
        return cur.execute(
            "SELECT handoff_id, session_id, reason_code, queue, created_at, picked_at"
            " FROM conv.handoff WHERE queue = %s ORDER BY created_at, handoff_id",
            (queue,),
        ).fetchall()


def get_handoff(
    conn: Conn, keys: KeyService, handoff_id: UUID
) -> tuple[HandoffRow, dict[str, Any]] | None:
    """The row and its decrypted briefing. Raises KeyDestroyed for a shredded subject."""
    found = conn.execute(
        "SELECT h.handoff_id, h.session_id, h.reason_code, h.queue, h.created_at, h.picked_at,"
        " h.payload_enc, s.key_ref FROM conv.handoff h JOIN conv.session s USING (session_id)"
        " WHERE h.handoff_id = %s",
        (handoff_id,),
    ).fetchone()
    if found is None:
        return None
    *columns, payload_enc, key_ref = found
    briefing = envelope.decrypt(keys.dek(key_ref), bytes(payload_enc), f"conv.handoff:{handoff_id}")
    return HandoffRow(*columns), json.loads(briefing)


def insert_kill_switch(
    conn: Conn, *, kind: str, target: str, active: bool, reason: str, actor: str
) -> UUID:
    switch_id = uuid7()
    conn.execute(
        "INSERT INTO conv.kill_switch (id, kind, target, active, reason, actor)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (switch_id, kind, target, active, reason, actor),
    )
    return switch_id


def active_kill_switches(conn: Conn) -> set[tuple[str, str]]:
    """(kind, target) whose newest row is active."""
    rows = conn.execute(
        "SELECT kind, target FROM (SELECT DISTINCT ON (kind, target) kind, target, active"
        " FROM conv.kill_switch ORDER BY kind, target, created_at DESC, id DESC) newest"
        " WHERE active"
    ).fetchall()
    return {(kind, target) for kind, target in rows}


def active_snapshots(conn: Conn) -> dict[str, str]:
    """collection -> the active corpus snapshot (the session's corpus pins)."""
    rows = conn.execute(
        "SELECT collection, snapshot_id FROM catalog.corpus_snapshot WHERE status = 'active'"
        " ORDER BY collection, built_at DESC"
    ).fetchall()
    pins: dict[str, str] = {}
    for collection, snapshot_id in rows:
        pins.setdefault(collection, snapshot_id)  # newest wins, should two ever be active
    return pins
