"""The session dossier (Step 24, TDD §4.3): rendered as compliance_ro from the audit and consent
hot store, verified against the chain first. Events are appended as app_rw and the consent record
is inserted as the superuser, then the role drops to compliance_ro, which is all the dossier needs
(plus the key service). Everything rolls back."""

import hashlib
import json
import stat
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
import pytest

from surakshasetu.audit.chain import append
from surakshasetu.audit.dossier import ERASED, run
from surakshasetu.audit.events import (
    ConsentCapturedHeader,
    DisclosureAckHeader,
    EngineDecisionHeader,
    EventType,
    Header,
    ResponseReleasedHeader,
    StateTransitionHeader,
    TurnInputHeader,
)
from surakshasetu.crypto.keys import LocalKeyService
from surakshasetu.logging import configure_logging
from surakshasetu.uuid7 import uuid7

pytestmark = pytest.mark.db

Conn = psycopg.Connection[tuple[Any, ...]]
H = hashlib.sha256(b"x").hexdigest()
SENTINEL = "SENTINEL-income-9876543"
PINS = {"prompt_bundle": "pb-2026.10.8", "rules": "2026.09.1"}


@pytest.fixture
def key_ref(keys: LocalKeyService) -> str:
    return keys.create_subject_key(uuid.uuid4())


def consent_record(db: Conn, session_id: UUID) -> UUID:
    """As the superuser, before SET ROLE: only domain_rw may write consent."""
    notice = f"dossier-test-{uuid7()}"
    db.execute(
        "INSERT INTO consent.notice_version (notice_version, language, body, body_sha256,"
        " approved_by, effective_from) VALUES (%s, 'en', 'DUMMY notice', %s, 'compliance',"
        " current_date)",
        (notice, bytes.fromhex(H)),
    )
    consent_id = uuid7()
    db.execute(
        "INSERT INTO consent.record (consent_id, subject_ref, session_id, notice_version, method,"
        " is_adult_declared, granted_at, notice_sha256, ai_disclosure_version, language)"
        " VALUES (%s, %s, %s, %s, 'structured_action', true, now(), %s, 'v1', 'en')",
        (consent_id, uuid7(), session_id, notice, bytes.fromhex(H)),
    )
    db.execute(
        "INSERT INTO consent.purpose_grant (consent_id, purpose, granted) VALUES"
        " (%s, 'P1', true), (%s, 'P2', false)",
        (consent_id, consent_id),
    )
    return consent_id


def session(db: Conn, keys: LocalKeyService, key_ref: str) -> UUID:
    """A short S0-S3 trail: consent, a customer turn, a decision, a release with its disclosure set,
    the acknowledgment, and a transition under a re-pinned bundle."""
    session_id = uuid7()
    consent_id = consent_record(db, session_id)
    db.execute("SET LOCAL ROLE app_rw")
    turn_id = uuid7()
    trail: list[tuple[EventType, Header, dict[str, Any], dict[str, Any]]] = [
        (
            EventType.CONSENT_CAPTURED,
            ConsentCapturedHeader(
                consent_id=consent_id,
                notice_version="2026.09.1-en",
                notice_sha256=H,
                purposes=["P1"],
                method="structured_action",
                language="en",
                adult_declared=True,
                captured_at="2026-10-08T10:00:00Z",  # type: ignore[arg-type]
            ),
            {"purposes": ["P1"]},
            PINS,
        ),
        (
            EventType.TURN_INPUT,
            TurnInputHeader(
                turn_id=turn_id, turn_seq=1, language="en", channel="web", turn_key=uuid7()
            ),
            {"stored_raw": f"I earn {SENTINEL} <script>alert(1)</script>", "redacted": "x"},
            PINS,
        ),
        (
            EventType.ENGINE_DECISION,
            EngineDecisionHeader(
                service="suitability",
                decision_id="dec-1",
                rules_version="2026.09.1",
                params_version="actuarial-2026.09.1",
                inputs_sha256=H,
                reason_codes=["FIT"],
            ),
            {"request": {"needs": {"annual_income_inr": SENTINEL}}, "result": {"outcome": "FIT"}},
            PINS,
        ),
        (
            EventType.RESPONSE_RELEASED,
            ResponseReleasedHeader(
                turn_id=uuid7(),
                rendered_sha256=H,
                citations=["E1"],
                verdicts={"release:RC-DUMMY": "count"},
                disclosure_set_sha256s=[H],
            ),
            {"response": {"message": {"text": "Here are your options."}}},
            PINS,
        ),
        (
            EventType.DISCLOSURE_ACK,
            DisclosureAckHeader(
                uin="999N001V02", registry_version="2026.09.1", set_sha256=H, document_sha256s=[H]
            ),
            {},
            PINS,
        ),
        (
            EventType.STATE_TRANSITION,
            StateTransitionHeader(
                from_state="S3", to_state="HANDOFF", trigger="S3.4", invariants={"I1": True}
            ),
            {},
            PINS | {"prompt_bundle": "pb-2026.10.9"},
        ),
    ]
    for event_type, header, payload, pins in trail:
        append(
            db,
            keys,
            session_id=session_id,
            event_type=event_type,
            fsm_state="S3",
            pins=pins,
            header=header,
            payload=payload,
            key_ref=key_ref,
        )
    db.execute("SET LOCAL ROLE compliance_ro")
    return session_id


def written(out: Path, session_id: UUID) -> tuple[dict[str, Any], str]:
    data, page = out / f"dossier-{session_id}.json", out / f"dossier-{session_id}.html"
    for path in (data, page):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # decrypted personal data
    return json.loads(data.read_text("utf-8")), page.read_text("utf-8")


def test_a_complete_session_renders_every_section(
    db: Conn, keys: LocalKeyService, key_ref: str, tmp_path: Path
) -> None:
    session_id = session(db, keys, key_ref)

    assert run(db, keys, session_id, tmp_path) == 0

    d, page = written(tmp_path, session_id)
    assert d["session_id"] == str(session_id)
    assert d["chain"] == {"ok": True, "checked": 6}
    assert [p["from_seq"] for p in d["pins"]] == [1, 6]
    assert d["pins"][1]["pins"]["prompt_bundle"] == "pb-2026.10.9"
    (record,) = d["consent"]["records"]
    assert record["session_id"] == str(session_id) and record["method"] == "structured_action"
    assert "record" not in record  # the HTML's table rows never leak into the JSON
    assert [(g["purpose"], g["granted"]) for g in record["grants"]] == [("P1", True), ("P2", False)]
    assert [e["event_type"] for e in d["consent"]["events"]] == ["CONSENT_CAPTURED"]
    assert [(t["seq"], t["event_type"]) for t in d["transcript"]] == [
        (2, "TURN_INPUT"),
        (4, "RESPONSE_RELEASED"),
    ]
    assert SENTINEL in d["transcript"][0]["text"]
    assert d["transcript"][1]["text"] == "Here are your options."
    (decision,) = d["decisions"]
    assert decision["header"]["rules_version"] == "2026.09.1"
    assert decision["header"]["params_version"] == "actuarial-2026.09.1"
    assert decision["payload"]["result"] == {"outcome": "FIT"}
    assert [e["event_type"] for e in d["disclosures"]] == ["RESPONSE_RELEASED", "DISCLOSURE_ACK"]
    assert d["disclosures"][1]["header"]["set_sha256"] == H
    assert [e["seq"] for e in d["events"]] == [1, 2, 3, 4, 5, 6]
    # The HTML is escaped: customer text can't inject markup into the inspector's page.
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page and "<script>alert" not in page
    assert "2026.09.1" in page and "DISCLOSURE_ACK" in page


def test_an_erased_session_keeps_headers_and_decisions_and_still_verifies(
    db: Conn, keys: LocalKeyService, key_ref: str, tmp_path: Path
) -> None:
    session_id = session(db, keys, key_ref)
    keys.destroy(key_ref)

    assert run(db, keys, session_id, tmp_path) == 0

    d, page = written(tmp_path, session_id)
    assert d["chain"] == {"ok": True, "checked": 6}
    assert {e["payload"] for e in d["events"]} == {ERASED}
    assert {t["text"] for t in d["transcript"]} == {ERASED}
    (decision,) = d["decisions"]
    assert decision["header"]["rules_version"] == "2026.09.1"
    assert decision["payload"] == ERASED
    assert d["disclosures"][1]["header"]["uin"] == "999N001V02"
    assert SENTINEL not in json.dumps(d) and SENTINEL not in page


def test_a_tampered_chain_is_refused_with_the_first_failing_link(
    db: Conn,
    keys: LocalKeyService,
    key_ref: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    session_id = session(db, keys, key_ref)
    db.execute("RESET ROLE")  # only the superuser can edit; the transaction rolls back
    db.execute(
        "UPDATE audit.audit_event SET header = jsonb_set(header, '{reason_codes}', '[\"NO_GAP\"]')"
        " WHERE session_id = %s AND seq = 3",
        (session_id,),
    )
    db.execute("SET LOCAL ROLE compliance_ro")

    assert run(db, keys, session_id, tmp_path) == 1

    assert "first bad seq 3" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_session_is_a_clear_error(
    db: Conn, keys: LocalKeyService, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db.execute("SET LOCAL ROLE compliance_ro")

    assert run(db, keys, uuid7(), tmp_path) == 2

    err = capsys.readouterr().err
    assert "no audit events" in err and "Traceback" not in err
    assert list(tmp_path.iterdir()) == []


@pytest.mark.usefixtures("restore_logging")
def test_no_payload_text_reaches_the_logs(
    db: Conn,
    keys: LocalKeyService,
    key_ref: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    logs, out = tmp_path / "logs", tmp_path / "out"
    configure_logging("DEBUG", logs)
    session_id = session(db, keys, key_ref)

    assert run(db, keys, session_id, out) == 0

    captured = capsys.readouterr()
    logged = captured.out + captured.err + "".join(p.read_text() for p in logs.iterdir())
    assert "dossier rendered" in logged  # logging ran
    assert SENTINEL not in logged and "Here are your options" not in logged
    assert SENTINEL in (out / f"dossier-{session_id}.json").read_text("utf-8")
