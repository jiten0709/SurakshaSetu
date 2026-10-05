"""store/conv.py against the migrated test database, as app_rw, inside a rolled-back transaction:
personal values encrypt under the subject key, slot history is append-only with the newest
confirmed row current, and the other conv tables take their rows. Run with `make check-db`."""

import hashlib
import os
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from runtime_support import pins

from surakshasetu.analysis.models import Intent, SlotCandidate, TurnAnalysis
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import EventType, ResponseReleasedHeader
from surakshasetu.crypto import envelope
from surakshasetu.crypto.keys import LocalKeyService
from surakshasetu.domain.models import RecommendedOption
from surakshasetu.graph.state import DisclosureAck, RecommendationPayload
from surakshasetu.store import conv as store
from surakshasetu.store.conv import Conn
from surakshasetu.uuid7 import uuid7

pytestmark = pytest.mark.db

H = hashlib.sha256(b"x").hexdigest()


def consent_record(db: Conn) -> UUID:
    """As the superuser, before SET ROLE: only domain_rw may write consent."""
    notice = f"store-test-{uuid7()}"
    db.execute(
        "INSERT INTO consent.notice_version (notice_version, language, body, body_sha256,"
        " approved_by, effective_from)"
        " VALUES (%s, 'en', 'DUMMY notice', %s, 'compliance', current_date)",
        (notice, bytes.fromhex(H)),
    )
    consent_id = uuid7()
    db.execute(
        "INSERT INTO consent.record (consent_id, subject_ref, session_id, notice_version, method,"
        " is_adult_declared, granted_at, notice_sha256, ai_disclosure_version, language)"
        " VALUES (%s, %s, %s, %s, 'structured_action', true, now(), %s, 'v1', 'en')",
        (consent_id, uuid7(), uuid7(), notice, bytes.fromhex(H)),
    )
    return consent_id


def new_session(db: Conn, keys: LocalKeyService) -> tuple[UUID, str]:
    session_id, subject_ref = uuid7(), uuid7()
    key_ref = keys.create_subject_key(subject_ref)
    store.insert_session(
        db,
        session_id=session_id,
        subject_ref=subject_ref,
        key_ref=key_ref,
        channel="web",
        locale="en-IN",
        pins=pins().model_dump(mode="json"),
        expires_at=datetime.now(UTC) + timedelta(days=30),
        token_sha256=os.urandom(32),
    )
    return session_id, key_ref


def turn(db: Conn, keys: LocalKeyService, key_ref: str, session_id: UUID, **update: Any) -> UUID:
    turn_id = uuid7()
    fields: dict[str, Any] = {
        "turn_id": turn_id,
        "session_id": session_id,
        "seq": 1,
        "direction": "in",
        "text": "I am 34 and my PAN is ABCDE1234F",
        "redacted": "I am 34 and my PAN is <PAN_1>",
        "language": "en",
        "analysis": None,
        "turn_key": uuid7(),
    }
    store.insert_turn(db, keys, key_ref, **(fields | update))
    return turn_id


def test_a_session_round_trips(db: Conn, keys: LocalKeyService) -> None:
    db.execute("SET ROLE app_rw")
    session_id, key_ref = new_session(db, keys)

    row = store.get_session(db, session_id, lock=True)

    assert row is not None and row.key_ref == key_ref and row.fsm_state == "S0"
    assert row.pins == pins().model_dump(mode="json") and row.counters == {}
    store.update_session(
        db,
        session_id,
        fsm_state="S1",
        frame_stack=[{"state": "S0"}],
        counters={"injection": 1},
        pins=row.pins,
        locale="hi-IN",
        status="active",
        consent_id=None,
    )
    updated = store.get_session(db, session_id)
    assert updated is not None and (updated.fsm_state, updated.locale) == ("S1", "hi-IN")
    assert updated.frame_stack == [{"state": "S0"}] and updated.counters == {"injection": 1}
    assert store.get_session(db, uuid7()) is None


def test_turn_text_is_encrypted_and_the_analysis_keeps_no_words(
    db: Conn, keys: LocalKeyService
) -> None:
    db.execute("SET ROLE app_rw")
    session_id, key_ref = new_session(db, keys)
    analysis = TurnAnalysis(
        intents=[Intent.SLOT_ANSWER],
        slots=[SlotCandidate(slot="age", value=34, confidence=0.9, evidence_span="I am 34")],
        side_query="what is 80C?",
        language="en",
    )

    turn_id = turn(db, keys, key_ref, session_id, analysis=store.analysis_projection(analysis))

    stored = db.execute(
        "SELECT text_enc, analysis FROM conv.turn WHERE turn_id = %s", (turn_id,)
    ).fetchone()
    assert stored is not None
    assert b"ABCDE1234F" not in bytes(stored[0])
    assert store.turn_text(db, keys, key_ref, turn_id) == "I am 34 and my PAN is ABCDE1234F"
    assert stored[1] == {
        "intents": ["SLOT_ANSWER"],
        "language": "en",
        "slots": [{"slot": "age", "confidence": 0.9}],
        "has_side_query": True,
    }
    assert store.last_seq(db, session_id) == 1


def test_slot_history_is_append_only_and_the_newest_confirmed_row_is_current(
    db: Conn, keys: LocalKeyService
) -> None:
    consent_id = consent_record(db)
    db.execute("SET ROLE app_rw")
    session_id, key_ref = new_session(db, keys)
    for slot, value, status in (
        ("age", 34, "confirmed"),
        ("age", 35, "proposed"),
        ("age", 36, "confirmed"),  # a correction is a new confirmed row
        ("annual_income_inr", None, "declined"),
        ("goals", ["income_protection"], "confirmed"),
    ):
        store.insert_slot(
            db,
            keys,
            key_ref,
            session_id=session_id,
            slot=slot,
            value=value,
            confidence=0.9,
            status=status,
            source_turn=None,
            consent_id=consent_id,
        )

    assert store.current_slots(db, keys, key_ref, session_id) == {
        "age": 36,
        "goals": ["income_protection"],
    }
    # Step 19: the newest row per slot, whatever its status.
    assert store.latest_slots(db, keys, key_ref, session_id) == {
        "age": ("confirmed", 36),
        "annual_income_inr": ("declined", None),
        "goals": ("confirmed", ["income_protection"]),
    }
    rows = db.execute(
        "SELECT count(*), count(evidence) FROM conv.slot_value WHERE session_id = %s",
        (session_id,),
    ).fetchone()
    assert rows == (5, 0)  # every row kept; no customer words in clear


def test_released_response_reads_the_committed_release(db: Conn, keys: LocalKeyService) -> None:
    db.execute("SET ROLE app_rw")
    session_id, key_ref = new_session(db, keys)
    key = uuid7()
    turn(db, keys, key_ref, session_id, turn_key=key)
    out_id = turn(db, keys, key_ref, session_id, seq=2, direction="out", turn_key=uuid7())
    body = {"turn_id": "t", "message": {"text": "Hi"}}
    audit_chain.append(
        db,
        keys,
        session_id=session_id,
        event_type=EventType.RESPONSE_RELEASED,
        fsm_state="S0",
        pins={},
        header=ResponseReleasedHeader(
            turn_id=out_id,
            rendered_sha256=H,
            citations=[],
            verdicts={},
            disclosure_set_sha256s=[],
        ),
        payload={"response": body},
        key_ref=key_ref,
    )

    assert store.released_response(db, keys, session_id, key) == body
    assert store.released_response(db, keys, session_id, uuid7()) is None


def test_recommendation_ack_handoff_and_kill_switch_rows(db: Conn, keys: LocalKeyService) -> None:
    db.execute("SET ROLE app_rw")
    session_id, key_ref = new_session(db, keys)
    option = RecommendedOption(
        rank=1,
        uin="999N001V02",
        sum_assured_inr="10000000",
        term_years=30,
        ppt_years=30,
        quote=None,
        reason_codes=["RANK-FIT"],
    )
    rec_id = store.insert_recommendation(
        db,
        session_id=session_id,
        payload=RecommendationPayload(
            options=[option],
            ranker_version="ranker-2026.09.1",
            suitability_inputs_sha256=H,
            evidence_map={},
            rendered_sha256=H,
        ),
    )
    ack = DisclosureAck(
        uin="999N001V02",
        registry_version="2026.09.1",
        disclosure_set_sha256=H,
        document_sha256={"CIS": H},
        acked_at=datetime.now(UTC),
    )
    store.insert_disclosure_ack(db, rec_id=rec_id, ack=ack)
    # Step 21: the application journey's intake row is not a person taking over.
    store.insert_handoff(
        db,
        keys,
        key_ref,
        session_id=session_id,
        reason_code="INTAKE_PENDING",
        queue="application",
        payload={"intake": {"signature": "DUMMY"}},
    )
    assert not store.has_handoff(db, session_id)
    handoff_id = store.insert_handoff(
        db,
        keys,
        key_ref,
        session_id=session_id,
        reason_code="HE_REQUEST",
        queue="advisors",
        payload={"briefing": "DUMMY"},
    )
    assert store.has_handoff(db, session_id)
    target = f"route-{uuid7()}"
    store.insert_kill_switch(db, kind="route", target=target, active=True, reason="R", actor="ops")

    assert ("route", target) in store.active_kill_switches(db)
    store.insert_kill_switch(db, kind="route", target=target, active=False, reason="R", actor="ops")
    assert ("route", target) not in store.active_kill_switches(db)
    stored = db.execute(
        "SELECT payload_enc FROM conv.handoff WHERE handoff_id = %s", (handoff_id,)
    ).fetchone()
    assert stored is not None
    plain = envelope.decrypt(keys.dek(key_ref), bytes(stored[0]), f"conv.handoff:{handoff_id}")
    assert plain == b'{"briefing":"DUMMY"}'
    options = db.execute(
        "SELECT options FROM conv.recommendation WHERE rec_id = %s", (rec_id,)
    ).fetchone()
    assert options is not None and options[0][0]["uin"] == "999N001V02"
