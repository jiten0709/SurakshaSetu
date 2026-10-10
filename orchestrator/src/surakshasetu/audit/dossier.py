"""The session dossier (TDD §4.3): `python -m surakshasetu.audit.dossier --session <id> --out DIR`.

For an inspection: the session's transcript, decisions with their rule and parameter versions,
disclosures and acknowledgments with their hashes, consent history, pins and the chain verification
result, as one JSON file and one HTML page. It renders exactly what the hot store holds (the audit
and consent schemas, read as compliance_ro; payloads decrypted through the key service), and
reconstructs nothing. The chain is verified first: a broken chain gets no dossier.

Payloads of a shredded subject read "[erased]"; headers stay, and the chain still verifies. The
files hold decrypted personal data, so they are written owner-only and never logged.
"""

import argparse
import base64
import html as html_lib
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import psycopg
from psycopg_pool import ConnectionPool

from surakshasetu.audit.chain import (
    AuditEvent,
    Conn,
    VerifyResult,
    decrypt_payload,
    events,
    verify_session,
)
from surakshasetu.config import ConfigError, load_settings
from surakshasetu.crypto.keys import KeyDestroyed, KeyService, LocalKeyService
from surakshasetu.logging import configure_logging

# Explicit name: run with -m, __name__ is "__main__" and would land in lib.log.
logger = logging.getLogger("surakshasetu.audit.dossier")

ERASED = "[erased]"
CONSENT = ("CONSENT_CAPTURED", "CONSENT_WITHDRAWN", "ERASURE_REQUEST")
TRANSCRIPT = ("TURN_INPUT", "RESPONSE_RELEASED")
DECISIONS = ("ENGINE_DECISION", "SUFFICIENCY_ELECTION")
DISCLOSURES = ("RESPONSE_RELEASED", "DISCLOSURE_ACK")


class UnknownSession(Exception):
    """The audit store holds no event for the session."""


class ChainRefused(Exception):
    """The chain failed verification (already logged CRITICAL by verify_session)."""

    def __init__(self, result: VerifyResult) -> None:
        super().__init__("chain broken")
        self.result = result


def build(conn: Conn, keys: KeyService, session_id: UUID) -> dict[str, Any]:
    verified = verify_session(conn, session_id)
    if not verified.ok:
        raise ChainRefused(verified)
    if verified.checked == 0:
        raise UnknownSession
    # Only what was verified: a live session may have appended since (seq is gap-free).
    trail = [_entry(keys, event) for event in events(conn, session_id)[: verified.checked]]
    pins: list[dict[str, Any]] = []
    for event in trail:
        if not pins or pins[-1]["pins"] != event["pins"]:
            pins.append({"from_seq": event["seq"], "pins": event["pins"]})
    records = [
        row[0]
        for row in conn.execute(
            "SELECT to_jsonb(r) || jsonb_build_object('grants', coalesce((SELECT"
            " jsonb_agg(to_jsonb(g) ORDER BY g.changed_at, g.purpose) FROM consent.purpose_grant g"
            " WHERE g.consent_id = r.consent_id), '[]'::jsonb))"
            " FROM consent.record r WHERE r.session_id = %s ORDER BY r.granted_at",
            (session_id,),
        ).fetchall()
    ]
    return {
        "session_id": str(session_id),
        "generated_at": datetime.now(UTC).isoformat(),
        "chain": {"ok": verified.ok, "checked": verified.checked},
        "pins": pins,
        "consent": {"records": records, "events": _only(trail, CONSENT)},
        "transcript": [
            {k: e[k] for k in ("seq", "event_type", "occurred_at", "fsm_state", "header")}
            | {"text": _text(e)}
            for e in _only(trail, TRANSCRIPT)
        ],
        "decisions": _only(trail, DECISIONS),
        "disclosures": [
            {k: v for k, v in e.items() if k != "payload"} for e in _only(trail, DISCLOSURES)
        ],
        "events": trail,
    }


def _entry(keys: KeyService, event: AuditEvent) -> dict[str, Any]:
    try:
        payload = decrypt_payload(keys, event)
    except KeyDestroyed:
        payload = ERASED
    return {
        "seq": event.seq,
        "event_type": event.event_type,
        "occurred_at": event.occurred_at.astimezone(UTC).isoformat(),
        "fsm_state": event.fsm_state,
        "pins": event.pins,
        "header": event.header,
        "payload": payload,
    }


def _only(trail: list[dict[str, Any]], types: tuple[str, ...]) -> list[dict[str, Any]]:
    return [e for e in trail if e["event_type"] in types]


def _text(entry: dict[str, Any]) -> str:
    """What was said, as stored: the customer's text (or the action or timer), or the released
    message."""
    payload = entry["payload"]
    if payload == ERASED:
        return ERASED
    if entry["event_type"] == "RESPONSE_RELEASED":
        return str(payload["response"]["message"]["text"])
    return str(payload["stored_raw"] if "stored_raw" in payload else json.dumps(payload))


def html(d: dict[str, Any]) -> str:
    def pre(value: Any) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=1)
        return f"<pre>{html_lib.escape(text)}</pre>"

    def table(rows: list[dict[str, Any]], columns: tuple[str, ...]) -> str:
        head = "".join(f"<th>{c}</th>" for c in columns)
        body = "".join(
            "<tr>" + "".join(f"<td>{pre(row.get(c, ''))}</td>" for c in columns) + "</tr>"
            for row in rows
        )
        return f"<table><tr>{head}</tr>{body}</table>" if rows else "<p>none</p>"

    event = ("seq", "event_type", "occurred_at", "fsm_state", "header", "payload")
    chain = d["chain"]
    sections = [
        ("Chain verification", f"<p>ok: {chain['ok']}, events verified: {chain['checked']}</p>"),
        ("Pins", table(d["pins"], ("from_seq", "pins"))),
        ("Consent records", table([{"record": r} for r in d["consent"]["records"]], ("record",))),
        ("Consent events", table(d["consent"]["events"], event)),
        ("Transcript", table(d["transcript"], ("seq", "event_type", "occurred_at", "text"))),
        ("Decisions", table(d["decisions"], event)),
        ("Disclosures and acknowledgments", table(d["disclosures"], event[:5])),
        ("Every event", table(d["events"], event)),
    ]
    title = f"Session dossier {html_lib.escape(d['session_id'])}"
    body = "".join(f"<h2>{name}</h2>{content}" for name, content in sections)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>'
        "<style>body{font:14px system-ui,sans-serif;margin:16px}table{border-collapse:collapse;"
        "width:100%}td,th{border:1px solid #999;padding:4px;vertical-align:top;text-align:left}"
        "pre{margin:0;white-space:pre-wrap;word-break:break-word}</style></head><body>"
        f"<h1>{title}</h1><p>Generated {html_lib.escape(d['generated_at'])} from the audit hot"
        f" store.</p>{body}</body></html>"
    )


def run(conn: Conn, keys: KeyService, session_id: UUID, out: Path) -> int:
    """0 with both files written; 1 for a broken chain and 2 for an unknown session, nothing
    written. Messages go to stderr; the dossier's content never reaches a log."""
    try:
        d = build(conn, keys, session_id)
    except ChainRefused as refused:
        r = refused.result
        link = f"first bad seq {r.first_bad_seq}" if r.gap_at is None else f"gap at seq {r.gap_at}"
        print(f"refused: the audit chain is broken: {link} ({r.checked} verified)", file=sys.stderr)
        return 1
    except UnknownSession:
        print(f"no audit events for session {session_id}", file=sys.stderr)
        return 2
    page = html(d)
    out.mkdir(parents=True, exist_ok=True)
    for suffix, content in (("json", json.dumps(d, ensure_ascii=False, indent=2)), ("html", page)):
        path = out / f"dossier-{session_id}.{suffix}"
        path.touch(mode=0o600)
        path.chmod(0o600)  # an existing file keeps its mode through touch
        path.write_text(content, encoding="utf-8")
        print(path)
    erased = sum(e["payload"] == ERASED for e in d["events"])
    logger.info("dossier rendered: %d events, %d erased", len(d["events"]), erased)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m surakshasetu.audit.dossier")
    parser.add_argument("--session", required=True, type=UUID, help="the session id")
    parser.add_argument("--out", type=Path, default=Path("."), help="directory for both files")
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    kek = base64.b64decode(settings.kek_b64.get_secret_value())
    with (
        psycopg.connect(settings.pg_dsn_compliance.get_secret_value()) as conn,
        ConnectionPool(settings.pg_dsn_keyvault.get_secret_value(), min_size=1) as vault,
    ):
        return run(conn, LocalKeyService(vault, kek), args.session, args.out)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ConfigError as exc:
        sys.exit(str(exc))
