"""The cross-cutting handlers against the migrated test database, committing for real: erasure
deletes every live conv row and the checkpoint as erasure_rw, keeps the audit chain, and schedules
(or, for a minor, performs) key destruction; a Consent Service outage leaves a pending withdrawal
that the sweep records later; hand-offs are P2-gated and their briefing redacted. The Consent
Service answers through httpx.MockTransport; its records exist in the test database for the FKs.
Run with `make check-db`."""

import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
import pytest_asyncio
from runtime_support import Models, db_runtime, domain_handler

from surakshasetu.api.app import create_app
from surakshasetu.audit.chain import decrypt_payload, events, verify_session
from surakshasetu.compose.bundle import load_bundle
from surakshasetu.crypto.keys import KeyDestroyed, LocalKeyService
from surakshasetu.graph.handlers import data_erasure
from surakshasetu.graph.runtime import ProblemError, Runtime
from surakshasetu.graph.state import DisclosureAck, RecommendationPayload
from surakshasetu.logging import configure_logging
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow
from surakshasetu.uuid7 import uuid7

pytestmark = [pytest.mark.db, pytest.mark.asyncio]

H = hashlib.sha256(b"x").hexdigest()
PII = "my PAN is ABCDE1234F, call me on 9876543210"
CONV = ("disclosure_ack", "recommendation", "handoff", "slot_value", "turn", "session")
SCRIPTS = load_bundle("pb-2026.10.8", env="test").templates["en-IN"].scripts


class ConsentService:
    """The Consent Service's record endpoints over one record per consent id. `down` answers the
    withdrawal with a 503, as an outage does."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.down = False
        self.calls: list[str] = []

    def add(self, consent_id: UUID, *, purposes: tuple[str, ...], adult: bool) -> None:
        self.records[str(consent_id)] = {
            "consent_id": str(consent_id),
            "notice_version": "2026.09.1-en",
            "notice_sha256": H,
            "notice_language": "en-IN",
            "ai_disclosure_version": "v1",
            "purposes": [
                {"purpose_id": p, "granted": p[:2] in purposes}
                for p in ("P1_NEEDS_RECO", "P2_ADVISOR_CONTACT", "P3_MARKETING")
            ],
            "age_18_plus_declared": adult,
            "method": "structured_action",
            "captured_at": datetime.now(UTC).isoformat(),
            "withdrawn_at": None,
            "valid_p1": adult and "P1" in purposes,
            "valid_reasons": [] if adult else ["AGE_NOT_DECLARED"],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/v1/consent/records/"):
            return domain_handler(request)
        consent_id, _, action = path.removeprefix("/v1/consent/records/").partition("/")
        self.calls.append(action or "get")
        record = self.records[consent_id]
        if action == "withdraw":
            if self.down:
                return httpx.Response(503, json={"title": "down", "status": 503})
            record |= {
                "withdrawn_at": datetime.now(UTC).isoformat(),
                "valid_p1": False,
                "valid_reasons": ["WITHDRAWN"],
            }
        elif action == "purposes":
            grant = json.loads(request.content)
            for purpose in record["purposes"]:
                if purpose["purpose_id"] == grant["purpose_id"]:
                    purpose["granted"] = grant["granted"]
        return httpx.Response(200, json=record)


@pytest.fixture
def consents() -> ConsentService:
    return ConsentService()


@pytest.fixture
def models() -> Models:
    """The analysis the next turn gets: tests set `intents` before a send."""
    return Models()


@pytest.fixture
def created(admin_dsn: str) -> Iterator[tuple[list[UUID], list[UUID]]]:
    """(sessions, consent ids) a test creates: whatever erasure left (audit rows, consent records)
    goes afterwards."""
    sessions: list[UUID] = []
    consent_ids: list[UUID] = []
    yield sessions, consent_ids
    with psycopg.connect(admin_dsn) as conn:
        conn.execute(
            "DELETE FROM conv.disclosure_ack WHERE rec_id IN (SELECT rec_id FROM"
            " conv.recommendation WHERE session_id = ANY(%s))",
            (sessions,),
        )
        for table in CONV[1:]:
            conn.execute(f"DELETE FROM conv.{table} WHERE session_id = ANY(%s)", (sessions,))  # noqa: S608
        conn.execute("DELETE FROM audit.audit_event WHERE session_id = ANY(%s)", (sessions,))
        threads = [str(s) for s in sessions]
        for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
            conn.execute(f"DELETE FROM langgraph.{table} WHERE thread_id = ANY(%s)", (threads,))  # noqa: S608
        conn.execute("DELETE FROM consent.purpose_grant WHERE consent_id = ANY(%s)", (consent_ids,))
        notices = conn.execute(
            "DELETE FROM consent.record WHERE consent_id = ANY(%s) RETURNING notice_version",
            (consent_ids,),
        ).fetchall()
        conn.execute(
            "DELETE FROM consent.notice_version WHERE notice_version = ANY(%s)",
            ([n[0] for n in notices],),
        )


@pytest_asyncio.fixture
async def runtime(
    keys: LocalKeyService, consents: ConsentService, models: Models
) -> AsyncIterator[Runtime]:
    async with db_runtime(keys, models=models, handler=consents) as rt:
        yield rt


async def session_with_consent(
    runtime: Runtime,
    admin_dsn: str,
    created: tuple[list[UUID], list[UUID]],
    consents: ConsentService,
    *,
    state: str = "S1",
    purposes: tuple[str, ...] | None = ("P1",),
    adult: bool = True,
) -> SessionRow:
    """A session as Step 18 will leave it: a consent record (in the database for the FKs, and in
    the stub service), attached, in `state`. With purposes None, no consent at all (S0)."""
    made = await runtime.create_session("web", "en-IN")
    session_id = UUID(made["session_id"])
    created[0].append(session_id)
    if purposes is not None:
        consent_id = uuid7()
        created[1].append(consent_id)
        notice = f"handlers-test-{consent_id}"
        with psycopg.connect(admin_dsn) as conn:
            conn.execute(
                "INSERT INTO consent.notice_version (notice_version, language, body, body_sha256,"
                " approved_by, effective_from)"
                " VALUES (%s, 'en', 'DUMMY notice', %s, 'compliance', current_date)",
                (notice, bytes.fromhex(H)),
            )
            conn.execute(
                "INSERT INTO consent.record (consent_id, subject_ref, session_id, notice_version,"
                " method, is_adult_declared, granted_at, notice_sha256, ai_disclosure_version,"
                " language)"
                " VALUES (%s, %s, %s, %s, 'structured_action', %s, now(), %s, 'v1', 'en')",
                (consent_id, uuid7(), session_id, notice, adult, bytes.fromhex(H)),
            )
            conn.execute(
                "UPDATE conv.session SET consent_id = %s, fsm_state = %s WHERE session_id = %s",
                (consent_id, state, session_id),
            )
        consents.add(consent_id, purposes=purposes, adult=adult)
    return await runtime.authenticate(session_id, made["session_token"])


async def send(runtime: Runtime, row: SessionRow, text: str | None = "hello", **action: Any) -> Any:
    body = await runtime.run_turn(row, uuid7(), text, action or None)
    return json.loads(body)


def counts(admin_dsn: str, session_id: UUID) -> dict[str, int]:
    with psycopg.connect(admin_dsn) as conn:
        found: dict[str, int] = {}
        for table in CONV[1:]:
            row = conn.execute(
                f"SELECT count(*) FROM conv.{table} WHERE session_id = %s",  # noqa: S608
                (session_id,),
            ).fetchone()
            found[table] = row[0] if row else 0
        acks = conn.execute(
            "SELECT count(*) FROM conv.disclosure_ack a JOIN conv.recommendation r USING (rec_id)"
            " WHERE r.session_id = %s",
            (session_id,),
        ).fetchone()
        found["disclosure_ack"] = acks[0] if acks else 0
        for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
            row = conn.execute(
                f"SELECT count(*) FROM langgraph.{table} WHERE thread_id = %s",  # noqa: S608
                (str(session_id),),
            ).fetchone()
            found[table] = row[0] if row else 0
    return found


def key_state(admin_dsn: str, key_ref: str) -> tuple[datetime | None, datetime | None]:
    with psycopg.connect(admin_dsn) as conn:
        row = conn.execute(
            "SELECT destroy_after, destroyed_at FROM keyvault.subject_key WHERE key_ref = %s",
            (key_ref,),
        ).fetchone()
    assert row is not None
    return row[0], row[1]


def chain(runtime: Runtime, session_id: UUID) -> list[Any]:
    with runtime.pool.connection() as conn:
        assert verify_session(conn, session_id).ok
        return events(conn, session_id)


def every_child_row(runtime: Runtime, row: SessionRow, consent_id: UUID) -> None:
    """One row in every conv table, as later steps will write them (app_rw, committed)."""
    with runtime.pool.connection() as conn:
        store.insert_slot(
            conn,
            runtime.keys,
            row.key_ref,
            session_id=row.session_id,
            slot="age",
            value=34,
            confidence=0.9,
            status="confirmed",
            source_turn=None,
            consent_id=consent_id,
        )
        rec_id = store.insert_recommendation(
            conn,
            session_id=row.session_id,
            payload=RecommendationPayload.model_construct(
                options=[], ranker_version="ranker-2026.09.1",
                suitability_inputs_sha256=H, rendered_sha256=H,
            ),
        )  # fmt: skip
        store.insert_disclosure_ack(
            conn,
            rec_id=rec_id,
            ack=DisclosureAck(
                uin="999N001V02",
                registry_version="2026.09.1",
                disclosure_set_sha256=H,
                document_sha256={"CIS": H},
                acked_at=datetime.now(UTC),
            ),
        )
        store.insert_handoff(
            conn,
            runtime.keys,
            row.key_ref,
            session_id=row.session_id,
            reason_code="HE_REQUEST",
            queue="advisor",
            payload={"reason_code": "HE_REQUEST"},
        )


# --- erasure --------------------------------------------------------------------------------------
async def test_erasure_deletes_every_live_row_and_the_checkpoint_and_keeps_the_audit_record(
    runtime: Runtime,
    admin_dsn: str,
    created: tuple[list[UUID], list[UUID]],
    consents: ConsentService,
    models: Models,
) -> None:
    row = await session_with_consent(runtime, admin_dsn, created, consents)
    await send(runtime, row)  # a committed turn and a checkpoint
    every_child_row(runtime, row, created[1][0])
    before = counts(admin_dsn, row.session_id)
    assert all(before[t] >= 1 for t in (*CONV, "checkpoints")), before

    models.intents = ("META_WITHDRAW",)
    released = await send(runtime, row, "please stop and delete everything")

    assert released["state"] == "DATA_ERASURE"
    assert released["message"]["text"] == SCRIPTS.erasure_done
    assert set(counts(admin_dsn, row.session_id).values()) == {0}
    types = [e.event_type for e in chain(runtime, row.session_id)]
    assert types.count("RESPONSE_RELEASED") == 2
    request = next(e for e in chain(runtime, row.session_id) if e.event_type == "ERASURE_REQUEST")
    assert request.header["consent_withdrawal"] == "done"
    assert "CONSENT_WITHDRAWN" in types and consents.calls.count("withdraw") == 1
    destroy_after, destroyed_at = key_state(admin_dsn, row.key_ref)
    expected = datetime.now(UTC) + timedelta(days=397)
    assert destroy_after is not None and abs((destroy_after - expected).total_seconds()) < 120
    assert destroyed_at is None
    with pytest.raises(ProblemError) as excinfo:  # gone: the same 401 as an unknown session
        await runtime.authenticate(row.session_id, "any")
    assert excinfo.value.status_code == 401


async def test_a_minor_is_erased_and_the_key_destroyed_at_once(
    runtime: Runtime,
    admin_dsn: str,
    created: tuple[list[UUID], list[UUID]],
    consents: ConsentService,
) -> None:
    row = await session_with_consent(runtime, admin_dsn, created, consents, adult=False)

    released = await send(runtime, row, "hello")

    assert released["state"] == "DATA_ERASURE"
    assert released["message"]["text"] == SCRIPTS.minor_exit
    assert set(counts(admin_dsn, row.session_id).values()) == {0}
    _, destroyed_at = key_state(admin_dsn, row.key_ref)
    assert destroyed_at is not None
    recorded = chain(runtime, row.session_id)  # still verifies without the key
    assert (
        next(e for e in recorded if e.event_type == "ERASURE_REQUEST").header["reason_code"]
        == "MINOR"
    )
    with pytest.raises(KeyDestroyed):
        decrypt_payload(runtime.keys, recorded[0])


async def test_a_consent_service_outage_erases_now_and_the_sweep_records_the_withdrawal(
    runtime: Runtime,
    admin_dsn: str,
    created: tuple[list[UUID], list[UUID]],
    consents: ConsentService,
    models: Models,
) -> None:
    row = await session_with_consent(runtime, admin_dsn, created, consents, state="S2")
    consents.down, models.intents = True, ("META_WITHDRAW",)

    released = await send(runtime, row, "withdraw my consent")

    assert released["message"]["text"] == SCRIPTS.erasure_pending
    assert set(counts(admin_dsn, row.session_id).values()) == {0}
    recorded = chain(runtime, row.session_id)
    request = next(e for e in recorded if e.event_type == "ERASURE_REQUEST")
    assert request.header["consent_withdrawal"] == "pending"
    assert "CONSENT_WITHDRAWN" not in [e.event_type for e in recorded]

    consents.down = False
    sweep = (runtime.pool, runtime.erasure, runtime.keys, runtime.domain, runtime.settings)
    await data_erasure.sweep(*sweep)
    await data_erasure.sweep(*sweep)  # recorded once only

    recorded = chain(runtime, row.session_id)
    assert [e.event_type for e in recorded].count("CONSENT_WITHDRAWN") == 1
    assert recorded[-1].event_type == "CONSENT_WITHDRAWN"


async def test_delete_takes_the_same_path_and_a_repeat_is_401(
    runtime: Runtime, admin_dsn: str, created: tuple[list[UUID], list[UUID]]
) -> None:
    app = create_app(runtime.settings, runtime=runtime)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as client:
        made = (
            await client.post("/v1/sessions", json={"channel": "web", "locale": "en-IN"})
        ).json()
        session_id = UUID(made["session_id"])
        created[0].append(session_id)
        auth = {"Authorization": f"Bearer {made['session_token']}"}

        first = await client.delete(f"/v1/sessions/{session_id}", headers=auth)
        again = await client.delete(f"/v1/sessions/{session_id}", headers=auth)

    assert first.status_code == 200
    assert first.json()["message"]["text"] == SCRIPTS.erasure_done  # no consent: none
    assert again.status_code == 401
    assert set(counts(admin_dsn, session_id).values()) == {0}
    types = [e.event_type for e in chain(runtime, session_id)]
    assert types[0] == "TURN_INPUT" and "ERASURE_REQUEST" in types


# --- hand-off -------------------------------------------------------------------------------------
async def test_the_handoff_waits_for_p2_and_its_briefing_is_redacted(
    runtime: Runtime,
    admin_dsn: str,
    created: tuple[list[UUID], list[UUID]],
    consents: ConsentService,
    models: Models,
) -> None:
    row = await session_with_consent(runtime, admin_dsn, created, consents)
    with runtime.pool.connection() as conn:  # a confirmed slot holding contact details
        store.insert_slot(
            conn, runtime.keys, row.key_ref, session_id=row.session_id, slot="note",
            value=PII, confidence=0.9, status="confirmed", source_turn=None,
            consent_id=created[1][0],
        )  # fmt: skip
    models.intents = ("META_HUMAN",)

    asked = await send(runtime, row, "can I talk to a person?")
    assert asked["state"] == "HUMAN_ESCALATION"
    assert asked["message"]["text"] == SCRIPTS.advisor_consent_ask
    assert counts(admin_dsn, row.session_id)["handoff"] == 0  # nothing shared yet

    handed = await send(runtime, row, None, type="ADVISOR_CONTACT", payload={"granted": True})
    assert handed["message"]["text"] == SCRIPTS.handoff
    assert consents.calls.count("purposes") == 1

    (queued,) = [h for h in await runtime.handoffs("advisor") if h.session_id == row.session_id]
    assert queued.reason_code == "HE_REQUEST"
    _, briefing = await runtime.handoff(queued.handoff_id)
    flat = json.dumps(briefing)
    assert "ABCDE1234F" not in flat and "9876543210" not in flat
    assert "<PAN_1>" in briefing["profile"]["note"]
    last = briefing["state_history"][-1]
    assert (last["from"], last["to"], last["reason_code"]) == (
        "S1",
        "HUMAN_ESCALATION",
        "HE_REQUEST",
    )
    assert briefing["consent_purposes"] == ["P1", "P2"]
    types = [e.event_type for e in chain(runtime, row.session_id)]
    assert "HANDOFF" in types and "CONSENT_CAPTURED" in types

    again = await send(runtime, row, None, type="ADVISOR_CONTACT", payload={"granted": True})
    assert again["message"]["text"] == SCRIPTS.handoff
    assert counts(admin_dsn, row.session_id)["handoff"] == 1  # handed off once


async def test_without_consent_an_escalation_shares_nothing(
    runtime: Runtime,
    admin_dsn: str,
    created: tuple[list[UUID], list[UUID]],
    consents: ConsentService,
    models: Models,
) -> None:
    row = await session_with_consent(runtime, admin_dsn, created, consents, purposes=None)
    models.intents = ("META_HUMAN",)

    released = await send(runtime, row, "I want a human")

    assert released["message"]["text"] == SCRIPTS.contact_options
    assert counts(admin_dsn, row.session_id)["handoff"] == 0
    assert consents.calls == []


@pytest.mark.usefixtures("restore_logging")
async def test_erasure_and_handoff_log_no_customer_data(
    runtime: Runtime,
    admin_dsn: str,
    created: tuple[list[UUID], list[UUID]],
    consents: ConsentService,
    models: Models,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG", tmp_path)
    row = await session_with_consent(runtime, admin_dsn, created, consents, purposes=("P1", "P2"))
    models.intents = ("META_HUMAN",)
    await send(runtime, row, PII)
    models.intents = ("META_WITHDRAW",)
    await send(runtime, row, PII)

    logged = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "handed off to advisor" in logged and "session erased" in logged
    for leaked in ("ABCDE1234F", "9876543210", row.key_ref, str(created[1][0])):
        assert leaked not in logged
