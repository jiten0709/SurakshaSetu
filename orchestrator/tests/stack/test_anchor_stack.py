"""The daily anchor against the compose stack: Postgres and MinIO with object lock.

Everything in Postgres runs in the `db` fixture's rolled-back transaction, today's anchor row
included. Only the MinIO object version persists; object lock keeps it for
SS_ANCHOR_RETENTION_DAYS. Run with `make up && make check-stack`.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
import pytest
from minio.commonconfig import COMPLIANCE
from minio.error import S3Error

from surakshasetu.audit.anchor import BUCKET, anchor_day, load_tsa_key, minio_client
from surakshasetu.audit.chain import append
from surakshasetu.audit.events import EventType, TurnInputHeader
from surakshasetu.config import Settings
from surakshasetu.crypto.keys import LocalKeyService
from surakshasetu.uuid7 import uuid7

pytestmark = pytest.mark.stack

Conn = psycopg.Connection[tuple[Any, ...]]


def test_anchor_writes_a_locked_object_and_a_chain_anchor_row(
    db: Conn, keys: LocalKeyService
) -> None:
    settings = Settings(_env_file=None)
    s3 = minio_client(settings)
    tsa_key = load_tsa_key(settings.tsa_key_path)
    today = datetime.now(UTC).date()
    # A superuser clears any earlier anchor for today; the rollback puts it back.
    db.execute("DELETE FROM audit.chain_anchor WHERE anchor_date = %s", (today,))
    db.execute("SET LOCAL ROLE app_rw")
    session_id, key_ref = uuid7(), keys.create_subject_key(uuid.uuid4())
    for seq in range(1, 4):
        append(
            db,
            keys,
            session_id=session_id,
            event_type=EventType.TURN_INPUT,
            fsm_state="S0",
            pins={"prompt": "2026.09.1"},
            header=TurnInputHeader(
                turn_id=uuid7(), turn_seq=seq, language="en", channel="web", turn_key=uuid7()
            ),
            payload={"text": "DUMMY hello"},
            key_ref=key_ref,
        )

    anchor = anchor_day(db, today, s3=s3, tsa_key=tsa_key, retention_days=1)

    assert anchor.worm_key is not None and anchor.sessions >= 1 and anchor.events >= 3
    name, version_id = anchor.worm_key.removeprefix(f"{BUCKET}/").split("?versionId=")
    retention = s3.get_object_retention(BUCKET, name, version_id=version_id)
    assert retention is not None and retention.mode == COMPLIANCE
    assert retention.retain_until_date > datetime.now(UTC) + timedelta(hours=23)
    with pytest.raises(S3Error):
        s3.remove_object(BUCKET, name, version_id=version_id)  # object lock refuses
    s3.stat_object(BUCKET, name, version_id=version_id)  # still there

    row = db.execute(
        "SELECT merkle_root, sessions, events, worm_key, tsa_token"
        " FROM audit.chain_anchor WHERE anchor_date = %s",
        (today,),
    ).fetchone()
    assert row is not None
    assert (bytes(row[0]), row[1], row[2], row[3]) == (
        anchor.root,
        anchor.sessions,
        anchor.events,
        anchor.worm_key,
    )
    tsa_key.public_key().verify(bytes(row[4]), anchor.root + today.isoformat().encode())

    # A re-run finds the same root and writes nothing new.
    assert anchor_day(db, today, s3=s3, tsa_key=tsa_key, retention_days=1).worm_key is None
