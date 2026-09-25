"""The audit hash chain and crypto-shredding, against the migrated test database.

Most cases append as app_rw inside the `db` fixture's rolled-back transaction. The concurrency
case has to commit across connections, so its 50 events stay in surakshasetu_test under a fresh
session, where `make verify-audit` can anchor them. Keys always commit: LocalKeyService writes
through its own keyvault_rw pool. Run with `make up && make check-db`.
"""

import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import errors

from surakshasetu.audit.anchor import ChainBroken, anchor_day, load_tsa_key
from surakshasetu.audit.chain import (
    GENESIS,
    SYSTEM_SESSION,
    VerifyResult,
    append,
    decrypt_payload,
    events,
    verify_session,
)
from surakshasetu.audit.events import ConfigReleaseHeader, EventType, StateTransitionHeader
from surakshasetu.crypto.keys import SYSTEM_KEY_REF, KeyDestroyed, LocalKeyService
from surakshasetu.logging import configure_logging
from surakshasetu.uuid7 import uuid7

pytestmark = pytest.mark.db

Conn = psycopg.Connection[tuple[Any, ...]]
SENTINEL = "SENTINEL-income-9876543"


def add(
    conn: Conn,
    keys: LocalKeyService,
    session_id: uuid.UUID,
    key_ref: str,
    count: int = 1,
    payload: dict[str, Any] | None = None,
) -> None:
    for i in range(count):
        append(
            conn,
            keys,
            session_id=session_id,
            event_type=EventType.STATE_TRANSITION,
            fsm_state="S1",
            pins={"prompt": "2026.09.1", "rules": "r-1"},
            header=StateTransitionHeader(
                from_state="S1", to_state="S2", trigger=f"S1-R{i}", invariants={"I1": True}
            ),
            payload=payload or {"note": f"event {i}"},
            key_ref=key_ref,
        )


@pytest.fixture
def app_db(db: Conn) -> Conn:
    db.execute("SET LOCAL ROLE app_rw")
    return db


@pytest.fixture
def key_ref(keys: LocalKeyService) -> str:
    return keys.create_subject_key(uuid.uuid4())


def test_a_chain_of_five_verifies(app_db: Conn, keys: LocalKeyService, key_ref: str) -> None:
    session_id = uuid7()
    add(app_db, keys, session_id, key_ref, count=5)

    chain = events(app_db, session_id)
    assert [event.seq for event in chain] == [1, 2, 3, 4, 5]
    assert chain[0].prev_hash == GENESIS
    assert all(
        later.prev_hash == earlier.hash for earlier, later in zip(chain, chain[1:], strict=False)
    )
    assert verify_session(app_db, session_id) == VerifyResult(ok=True, checked=5)
    assert decrypt_payload(keys, chain[2]) == {"note": "event 2"}


@pytest.mark.parametrize(
    "edit",
    [
        "UPDATE audit.audit_event SET header = jsonb_set(header, '{trigger}', '\"S9-R9\"')"
        " WHERE session_id = %s AND seq = 3",
        "UPDATE audit.audit_event SET fsm_state = 'S3' WHERE session_id = %s AND seq = 3",
        "UPDATE audit.audit_event SET occurred_at = occurred_at - interval '1 day'"
        " WHERE session_id = %s AND seq = 3",
    ],
    ids=["header", "fsm_state", "occurred_at"],
)
def test_tampering_names_the_first_bad_seq(
    app_db: Conn, keys: LocalKeyService, key_ref: str, edit: Any
) -> None:
    session_id = uuid7()
    add(app_db, keys, session_id, key_ref, count=5)

    app_db.execute("RESET ROLE")  # only the superuser can edit; the transaction rolls back
    app_db.execute(edit, (session_id,))

    assert verify_session(app_db, session_id) == VerifyResult(ok=False, checked=2, first_bad_seq=3)


def test_a_missing_event_is_a_gap(app_db: Conn, keys: LocalKeyService, key_ref: str) -> None:
    session_id = uuid7()
    add(app_db, keys, session_id, key_ref, count=5)

    app_db.execute("RESET ROLE")
    app_db.execute("DELETE FROM audit.audit_event WHERE session_id = %s AND seq = 3", (session_id,))

    assert verify_session(app_db, session_id) == VerifyResult(ok=False, checked=2, gap_at=3)


def test_the_anchor_refuses_a_day_with_a_broken_chain(
    app_db: Conn, keys: LocalKeyService, key_ref: str
) -> None:
    session_id = uuid7()
    add(app_db, keys, session_id, key_ref, count=2)
    app_db.execute("RESET ROLE")
    app_db.execute(
        "UPDATE audit.audit_event SET fsm_state = 'S3' WHERE session_id = %s AND seq = 2",
        (session_id,),
    )

    with pytest.raises(ChainBroken):  # raised before anything is written, so no MinIO needed
        anchor_day(
            app_db, datetime.now(UTC).date(), s3=None, tsa_key=load_tsa_key(None), retention_days=1
        )


@pytest.mark.parametrize("anchored_after_close", [True, False])
def test_a_changed_root_pages_only_if_the_day_was_anchored_after_it_closed(
    db: Conn, caplog: pytest.LogCaptureFixture, anchored_after_close: bool
) -> None:
    day = date(2001, 1, 1)  # no events, so its root is SHA-256(b"")
    anchored_at = (
        datetime(2001, 1, 2, 3, tzinfo=UTC)
        if anchored_after_close
        else datetime(2001, 1, 1, 12, tzinfo=UTC)
    )
    db.execute(
        "INSERT INTO audit.chain_anchor (anchor_date, merkle_root, sessions, events, worm_key,"
        " created_at) VALUES (%s, %s, 0, 0, 'audit-anchors/2001-01-01.json', %s)",
        (day, bytes(32), anchored_at),
    )
    db.execute("SET LOCAL ROLE app_rw")
    run = {"s3": None, "tsa_key": load_tsa_key(None), "retention_days": 1}

    if anchored_after_close:
        with pytest.raises(ChainBroken):
            anchor_day(db, day, **run)
        assert any(
            r.levelno == logging.CRITICAL and "AUDIT_CHAIN_BROKEN" in r.getMessage()
            for r in caplog.records
        )
    else:
        assert anchor_day(db, day, **run).worm_key is None  # a warning; the row can't change


def test_concurrent_appends_to_one_session_serialise(
    admin_dsn: str, keys: LocalKeyService, key_ref: str
) -> None:
    session_id = uuid7()
    start = threading.Barrier(2)

    def writer(_: int) -> None:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute("SET ROLE app_rw")
            start.wait()
            for _ in range(25):
                with conn.transaction():
                    add(conn, keys, session_id, key_ref)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(writer, range(2)))  # list() re-raises a writer's exception

    with psycopg.connect(admin_dsn) as conn:
        assert [event.seq for event in events(conn, session_id)] == list(range(1, 51))
        assert verify_session(conn, session_id) == VerifyResult(ok=True, checked=50)


def test_other_sessions_do_not_wait_for_a_held_lock(
    admin_dsn: str, keys: LocalKeyService, key_ref: str
) -> None:
    held, other = uuid7(), uuid7()
    with (
        psycopg.connect(admin_dsn, autocommit=True) as a,
        psycopg.connect(admin_dsn, autocommit=True) as b,
    ):
        a.execute("SET ROLE app_rw")
        b.execute("SET ROLE app_rw")
        b.execute("SET lock_timeout = '1s'")
        with a.transaction(force_rollback=True):
            add(a, keys, held, key_ref)  # a holds `held`'s lock until it rolls back

            with b.transaction(force_rollback=True):
                add(b, keys, other, key_ref)
            with pytest.raises(errors.LockNotAvailable), b.transaction(force_rollback=True):
                add(b, keys, held, key_ref)


def test_a_destroyed_key_shreds_the_payload_but_the_chain_still_verifies(
    app_db: Conn, keys: LocalKeyService, key_ref: str
) -> None:
    session_id = uuid7()
    add(app_db, keys, session_id, key_ref, count=3, payload={"income": SENTINEL})
    first = events(app_db, session_id)[0]
    assert decrypt_payload(keys, first) == {"income": SENTINEL}

    keys.destroy(key_ref)

    with pytest.raises(KeyDestroyed):
        decrypt_payload(keys, first)
    assert verify_session(app_db, session_id) == VerifyResult(ok=True, checked=3)


def test_destroy_due_shreds_only_keys_past_their_date(keys: LocalKeyService) -> None:
    due, later = keys.create_subject_key(uuid.uuid4()), keys.create_subject_key(uuid.uuid4())
    now = datetime.now(UTC)
    keys.schedule_destruction(due, now - timedelta(seconds=1))
    keys.schedule_destruction(later, now + timedelta(days=30))
    keys.dek(due)  # cached: destroy_due must evict it too

    assert keys.destroy_due(now) >= 1
    with pytest.raises(KeyDestroyed):
        keys.dek(due)
    assert len(keys.dek(later)) == 32


def test_system_events_use_the_system_chain_and_only_they_do(
    app_db: Conn, keys: LocalKeyService, key_ref: str
) -> None:
    release = ConfigReleaseHeader(
        artefact="prompt_bundle", version="2026.09.1", sha256="ab" * 32, approvals_count=2
    )
    system = {
        "fsm_state": "SYSTEM",
        "pins": {},
        "payload": {"approvers": ["ops-1", "compliance-2"]},
    }
    event = append(
        app_db,
        keys,
        session_id=SYSTEM_SESSION,
        event_type=EventType.CONFIG_RELEASE,
        header=release,
        key_ref=SYSTEM_KEY_REF,
        **system,
    )
    assert decrypt_payload(keys, event) == system["payload"]
    assert verify_session(app_db, SYSTEM_SESSION).ok

    with pytest.raises(ValueError, match="system chain"):
        append(
            app_db,
            keys,
            session_id=uuid7(),
            event_type=EventType.CONFIG_RELEASE,
            header=release,
            key_ref=key_ref,
            **system,
        )
    with pytest.raises(ValueError, match="system chain"):
        add(app_db, keys, uuid7(), SYSTEM_KEY_REF)
    with pytest.raises(TypeError, match="ConfigReleaseHeader"):
        append(
            app_db,
            keys,
            session_id=SYSTEM_SESSION,
            event_type=EventType.CONFIG_RELEASE,
            header=StateTransitionHeader(
                from_state="S1", to_state="S2", trigger="x", invariants={}
            ),
            key_ref=SYSTEM_KEY_REF,
            **system,
        )


def test_append_refuses_an_autocommit_connection_outside_a_transaction(
    admin_dsn: str, keys: LocalKeyService, key_ref: str
) -> None:
    with (
        psycopg.connect(admin_dsn, autocommit=True) as conn,
        pytest.raises(RuntimeError, match="transaction"),
    ):
        add(conn, keys, uuid7(), key_ref)


@pytest.mark.usefixtures("restore_logging")
def test_no_payload_key_or_dek_reaches_the_logs(
    app_db: Conn,
    keys: LocalKeyService,
    key_ref: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG", tmp_path)
    dek = keys.dek(key_ref)
    session_id = uuid7()

    add(app_db, keys, session_id, key_ref, count=2, payload={"income": SENTINEL})
    decrypt_payload(keys, events(app_db, session_id)[0])
    keys.destroy(key_ref)
    verify_session(app_db, session_id)

    logged = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.iterdir())
    assert "audit append seq=2" in logged and "subject key destroyed" in logged  # logging ran
    for leaked in (SENTINEL, key_ref, dek.hex(), repr(dek)):
        assert leaked not in logged
