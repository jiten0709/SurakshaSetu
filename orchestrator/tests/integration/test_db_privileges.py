"""The Step 2 privilege model, proven against the migrated test database.

Each case switches role with SET LOCAL ROLE inside a transaction that is always rolled back, so
nothing persists. Run with `make up && make check-db`.
"""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg import errors, sql
from psycopg.abc import Query

pytestmark = pytest.mark.db

Conn = psycopg.Connection[tuple[Any, ...]]

LOGIN_ROLES = [
    "app_rw",
    "domain_rw",
    "catalog_loader",
    "erasure_rw",
    "keyvault_rw",
    "compliance_ro",
]

TEMPLATES = {
    "SELECT": sql.SQL("SELECT 1 FROM {}"),
    "INSERT": sql.SQL("INSERT INTO {} DEFAULT VALUES"),
    "DELETE": sql.SQL("DELETE FROM {} WHERE false"),
    "TRUNCATE": sql.SQL("TRUNCATE {}"),
}

# UPDATE and DELETE use WHERE false: Postgres checks privileges before it looks at any row.
DENIED = [
    ("app_rw", "UPDATE audit.audit_event SET event_type = 'x' WHERE false"),
    ("app_rw", "DELETE FROM audit.audit_event WHERE false"),
    ("app_rw", "TRUNCATE audit.audit_event"),
    ("app_rw", "UPDATE conv.slot_value SET status = 'corrected' WHERE false"),
    ("app_rw", "DELETE FROM conv.slot_value WHERE false"),
    ("app_rw", "INSERT INTO consent.record DEFAULT VALUES"),
    ("app_rw", "UPDATE consent.record SET withdrawn_at = now() WHERE false"),
    ("app_rw", "INSERT INTO catalog.product DEFAULT VALUES"),
    ("domain_rw", "UPDATE audit.audit_event SET event_type = 'x' WHERE false"),
    ("domain_rw", "UPDATE conv.session SET fsm_state = 'x' WHERE false"),
    ("domain_rw", "UPDATE consent.record SET language = 'x' WHERE false"),
]

# Controls: without these, a role missing its schema USAGE would pass every denial above.
ALLOWED = [
    ("app_rw", "SELECT 1 FROM consent.record"),
    ("app_rw", "SELECT 1 FROM catalog.product"),
    ("app_rw", "DELETE FROM langgraph.checkpoints WHERE false"),
    ("domain_rw", "UPDATE consent.record SET withdrawn_at = now() WHERE false"),
    ("domain_rw", "SELECT 1 FROM catalog.product"),
    ("catalog_loader", "DELETE FROM catalog.product WHERE false"),
    ("erasure_rw", "DELETE FROM conv.slot_value WHERE false"),
    ("erasure_rw", "DELETE FROM langgraph.checkpoints WHERE false"),
    ("keyvault_rw", "DELETE FROM keyvault.subject_key WHERE false"),
    ("compliance_ro", "SELECT 1 FROM audit.audit_event"),
    ("compliance_ro", "SELECT 1 FROM consent.record"),
]

U = uuid.uuid4()
H = bytes(32)  # also the genesis prev_hash: 32 zero bytes
NOW = datetime.now(UTC)
# CHECK runs before foreign keys are enforced, so the dangling ids here never get that far.
CHECKS = [
    (
        "consent.record",
        {
            "consent_id": U,
            "subject_ref": U,
            "session_id": U,
            "notice_version": "2026.09.1-en",
            "method": "email",
            "is_adult_declared": True,
            "granted_at": NOW,
            "notice_sha256": H,
            "ai_disclosure_version": "v1",
            "language": "en",
        },
        "record_method_check",
    ),
    (
        "consent.purpose_grant",
        {"consent_id": U, "purpose": "P9", "granted": True},
        "purpose_grant_purpose_check",
    ),
    (
        "conv.turn",
        {
            "turn_id": U,
            "session_id": U,
            "seq": 1,
            "direction": "sideways",
            "text_enc": H,
            "redacted": "x",
            "language": "en",
            "turn_key": U,
        },
        "turn_direction_check",
    ),
    (
        "conv.slot_value",
        {
            "session_id": U,
            "slot": "age",
            "value_enc": H,
            "confidence": Decimal("0.900"),
            "status": "gone",
            "consent_id": U,
        },
        "slot_value_status_check",
    ),
    (
        "catalog.product_document",
        {
            "uin": "999N001V02",
            "kind": "BROCHURE",
            "version": "1",
            "language": "en",
            "uri": "x",
            "sha256": H,
        },
        "product_document_kind_check",
    ),
]


@pytest.fixture
def db() -> Iterator[Conn]:
    dsn = os.environ.get("SS_TEST_PG_DSN_ADMIN")
    if not dsn:
        pytest.fail("SS_TEST_PG_DSN_ADMIN is not set; run `make up && make check-db`")
    # A superuser session can SET ROLE to any role; force_rollback discards every write.
    with psycopg.connect(dsn, autocommit=True) as conn, conn.transaction(force_rollback=True):
        yield conn


def set_role(conn: Conn, role: str) -> None:
    conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))


def outcome(conn: Conn, role: str, statement: Query) -> str:
    """'denied', 'allowed', or the error raised instead. Leaves no trace either way."""
    try:
        with conn.transaction(force_rollback=True):  # savepoint: undoes the role switch too
            set_role(conn, role)
            conn.execute(statement)
    except errors.InsufficientPrivilege:
        return "denied"
    except psycopg.Error as exc:
        return type(exc).__name__
    return "allowed"


def tables(conn: Conn, *schemas: str) -> list[tuple[str, str]]:
    """Tables in `schemas`, or in every non-system schema when none are given."""
    rows = conn.execute(
        "SELECT schemaname, tablename FROM pg_tables"
        " WHERE schemaname NOT IN ('pg_catalog', 'information_schema')"
        " AND (cardinality(%s::text[]) = 0 OR schemaname = ANY(%s::text[]))"
        " ORDER BY 1, 2",
        (list(schemas), list(schemas)),
    ).fetchall()
    assert rows, f"no tables in {schemas or 'any schema'}; run `make db-migrate`"
    return [(schema, table) for schema, table in rows]


def not_denied(
    conn: Conn, role: str, found: list[tuple[str, str]], verbs: list[str]
) -> dict[str, str]:
    results = {
        f"{verb} {schema}.{table}": outcome(
            conn, role, TEMPLATES[verb].format(sql.Identifier(schema, table))
        )
        for schema, table in found
        for verb in verbs
    }
    return {statement: result for statement, result in results.items() if result != "denied"}


def new_consent(conn: Conn) -> uuid.UUID:
    notice_version = f"test-{uuid.uuid4()}"
    conn.execute(
        "INSERT INTO consent.notice_version (notice_version, language, body, body_sha256,"
        " approved_by, effective_from)"
        " VALUES (%s, 'en', 'DUMMY notice', %s, 'compliance', current_date)",
        (notice_version, H),
    )
    consent_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO consent.record (consent_id, subject_ref, session_id, notice_version, method,"
        " is_adult_declared, granted_at, notice_sha256, ai_disclosure_version, language)"
        " VALUES (%s, %s, %s, %s, 'structured_action', true, now(), %s, 'v1', 'en')",
        (consent_id, uuid.uuid4(), uuid.uuid4(), notice_version, H),
    )
    return consent_id


def new_session(conn: Conn, consent_id: uuid.UUID | None = None) -> uuid.UUID:
    session_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO conv.session (session_id, subject_ref, key_ref, channel, fsm_state, pins,"
        " expires_at, token_sha256, consent_id)"
        " VALUES (%s, %s, 'kms/subject-key', 'web', 'S0', '{}', now() + interval '1 hour', %s, %s)",
        (session_id, uuid.uuid4(), os.urandom(32), consent_id),
    )
    return session_id


def new_slot(conn: Conn, session_id: uuid.UUID, consent_id: uuid.UUID) -> None:
    # slot_value_id is a bigserial, so this also proves app_rw may use the sequence.
    conn.execute(
        "INSERT INTO conv.slot_value (session_id, slot, value_enc, confidence, status, consent_id)"
        " VALUES (%s, 'age', %s, 0.900, 'confirmed', %s)",
        (session_id, H, consent_id),
    )


@pytest.mark.parametrize(("role", "statement"), DENIED)
def test_denied(db: Conn, role: str, statement: Any) -> None:
    assert outcome(db, role, statement) == "denied"


@pytest.mark.parametrize(("role", "statement"), ALLOWED)
def test_allowed(db: Conn, role: str, statement: Any) -> None:
    assert outcome(db, role, statement) == "allowed"


def test_domain_rw_cannot_touch_audit_or_conv(db: Conn) -> None:
    assert not_denied(db, "domain_rw", tables(db, "audit", "conv"), list(TEMPLATES)) == {}


def test_compliance_ro_cannot_insert_anywhere(db: Conn) -> None:
    assert not_denied(db, "compliance_ro", tables(db), ["INSERT"]) == {}


@pytest.mark.parametrize("role", [role for role in LOGIN_ROLES if role != "domain_rw"])
def test_only_domain_rw_writes_consent(db: Conn, role: str) -> None:
    assert not_denied(db, role, tables(db, "consent"), ["INSERT", "DELETE", "TRUNCATE"]) == {}


def test_app_rw_appends_to_audit(db: Conn) -> None:
    session_id = uuid.uuid4()
    set_role(db, "app_rw")
    db.execute(
        "INSERT INTO audit.audit_event (event_id, session_id, seq, event_type, occurred_at,"
        " fsm_state, pins, header, payload_enc, key_ref, prev_hash, hash)"
        " VALUES (%s, %s, 1, 'test', now(), 'S0', '{}', '{}', %s, 'k', %s, %s)",
        (uuid.uuid4(), session_id, b"\x00", H, H),
    )
    count = db.execute(
        "SELECT count(*) FROM audit.audit_event WHERE session_id = %s", (session_id,)
    ).fetchone()
    assert count == (1,)


def test_slot_without_consent_is_rejected(db: Conn) -> None:
    """I1 backstop: even with INSERT on slot_value, a slot needs a real consent record."""
    set_role(db, "app_rw")
    session_id = new_session(db)
    with pytest.raises(errors.ForeignKeyViolation) as excinfo:
        new_slot(db, session_id, consent_id=uuid.uuid4())
    assert excinfo.value.diag.constraint_name == "slot_value_consent_id_fkey"


def test_slot_with_consent_is_accepted(db: Conn) -> None:
    consent_id = new_consent(db)
    set_role(db, "app_rw")
    new_slot(db, new_session(db, consent_id), consent_id)


def test_domain_rw_records_and_withdraws_consent(db: Conn) -> None:
    set_role(db, "domain_rw")
    consent_id = new_consent(db)
    db.execute(
        "UPDATE consent.record SET withdrawn_at = now() WHERE consent_id = %s", (consent_id,)
    )


@pytest.mark.parametrize(("table", "row", "constraint"), CHECKS, ids=[c[2] for c in CHECKS])
def test_check_constraint(db: Conn, table: str, row: dict[str, Any], constraint: str) -> None:
    statement = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(*table.split(".")),
        sql.SQL(", ").join(map(sql.Identifier, row)),
        sql.SQL(", ").join(sql.Placeholder() * len(row)),
    )
    with pytest.raises(errors.CheckViolation) as excinfo:
        db.execute(statement, list(row.values()))
    assert excinfo.value.diag.constraint_name == constraint


def test_checkpointer_tables_are_in_langgraph_owned_by_app_rw(db: Conn) -> None:
    owner = db.execute(
        "SELECT tableowner FROM pg_tables"
        " WHERE schemaname = 'langgraph' AND tablename = 'checkpoints'"
    ).fetchone()
    assert owner == ("app_rw",)


def test_public_schema_is_empty(db: Conn) -> None:
    count = db.execute(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = 'public'"
    ).fetchone()
    assert count == (0,)


def test_audit_tables_are_owned_by_audit_owner(db: Conn) -> None:
    owners = db.execute("SELECT DISTINCT tableowner FROM pg_tables WHERE schemaname = 'audit'")
    assert owners.fetchall() == [("audit_owner",)]
