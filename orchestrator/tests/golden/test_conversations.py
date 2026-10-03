"""Plays every golden conversation against the running stack (`make eval`; marker golden).

The orchestrator runs in-process with its real lifespan (Runtime.open). Around it is the real
stack: the dev database (domain-services writes consent there, so the FKs hold), valkey,
domain-services, and OmniRoute in front of the stubs, which each turn's `script` programs through
/__script. A `given` block is applied as superuser before the first turn. After the run, the
transcript (audit events, released bodies, conv snapshots, logs, advisor briefings) goes through the
per-turn expectations and the global assertions (harness.check). Then everything the conversation
created is deleted.

Needs `make up`, `make gateway-up` and `make seed-catalog`; `make eval` passes the DSNs and tokens.
"""

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from harness import (
    ROUTES,
    Conversation,
    Event,
    Sent,
    Transcript,
    Turn,
    check,
    load_conversations,
    part_ids,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.types.json import Jsonb
from qdrant_client import AsyncQdrantClient, models

from surakshasetu.api.app import create_app
from surakshasetu.audit.chain import AuditEvent, decrypt_payload
from surakshasetu.config import Settings
from surakshasetu.crypto.keys import KeyDestroyed
from surakshasetu.domain.models import ConsentRecordCreate, PurposeGrant
from surakshasetu.graph.nodes import build_graph
from surakshasetu.graph.runtime import Runtime
from surakshasetu.uuid7 import uuid7

pytestmark = [pytest.mark.golden, pytest.mark.asyncio]

STUBS = os.environ.get("SS_TEST_STUBS_URL", "http://127.0.0.1:8090")
SYSTEM = UUID(int=0)
PURPOSES = {"P1": "P1_NEEDS_RECO", "P2": "P2_ADVISOR_CONTACT", "P3": "P3_MARKETING"}
CONV_TABLES = ("recommendation", "handoff", "slot_value", "turn", "session")
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")


def admin_dsn() -> str:
    dsn = os.environ.get("SS_EVAL_PG_DSN_ADMIN")
    if not dsn:
        pytest.fail(
            "SS_EVAL_PG_DSN_ADMIN is not set; run `make up && make gateway-up && make eval`"
        )
    return dsn


class NoCheckpoint(AsyncPostgresSaver):
    """The process dies after the commit: the checkpoint is never written."""

    async def aput(self, config: Any, checkpoint: Any, metadata: Any, new_versions: Any) -> Any:
        return {"configurable": {**config["configurable"], "checkpoint_id": checkpoint["id"]}}

    async def aput_writes(self, *args: Any, **kwargs: Any) -> None:
        return None


class Play:
    """One conversation against the stack."""

    def __init__(self, app: FastAPI, settings: Settings, conversation: Conversation, logs: Path):
        self.app, self.settings, self.conversation, self.log_dir = app, settings, conversation, logs
        self.runtime: Runtime = app.state.runtime
        self.api = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://orchestrator.test"
        )
        self.stubs = httpx.AsyncClient(base_url=STUBS, timeout=10)
        self.db = psycopg.connect(admin_dsn(), autocommit=True)
        self.failures: list[str] = []
        self.session_id: UUID | None = None
        self.key_ref = ""
        self.switches: list[str] = []
        self.system_seq: int | None = None
        self.last: tuple[int, Turn, UUID] | None = None  # the previous request, for a retry

    def fail(self, turn: int, message: str) -> None:
        self.failures.append(f"{self.conversation.id} turn {turn}: {message}")

    def one(self, sql: str, *params: Any) -> Any:
        row = self.db.execute(sql, params).fetchone()
        return None if row is None else row[0]

    # --- setup -------------------------------------------------------------------------------
    async def open(self) -> Transcript:
        c = self.conversation
        made = await self.api.post("/v1/sessions", json={"channel": c.channel, "locale": c.locale})
        assert made.status_code == 201, f"session not created: {made.status_code} {made.text}"
        created = made.json()
        self.session_id, self.token = UUID(created["session_id"]), created["session_token"]
        subject_ref, self.key_ref = self.db.execute(
            "SELECT subject_ref, key_ref FROM conv.session WHERE session_id = %s",
            (self.session_id,),
        ).fetchone()  # type: ignore[misc]
        valid = await self.given(subject_ref)
        products = await self.runtime.domain.list_products()
        return Transcript(
            conversation=c,
            active_bundle=self.settings.prompt_bundle,
            consent_valid_from_start=valid,
            initial_pins=self.one(
                "SELECT pins FROM conv.session WHERE session_id = %s", self.session_id
            ),
            product_names=[p.name for p in products],
        )

    async def given(self, subject_ref: UUID) -> bool:
        """Apply the `given` block as superuser. True when a valid P1 exists from the start."""
        g, sets, params = self.conversation.given, [], []
        valid = False
        if g.consent is not None:
            notice = await self.runtime.domain.get_current_consent_notice(self.conversation.locale)
            record = await self.runtime.domain.create_consent_record(
                ConsentRecordCreate(
                    session_id=self.session_id,
                    subject_ref=subject_ref,
                    notice_version=notice.notice_version,
                    notice_sha256=notice.body_sha256,
                    language=self.conversation.locale,
                    ai_disclosure_version="2026.09.1",
                    purposes=[
                        PurposeGrant(purpose_id=pid, granted=p in g.consent.purposes)  # type: ignore[arg-type]
                        for p, pid in PURPOSES.items()
                    ],
                    age_18_plus_declared=g.consent.adult,
                    method="structured_action",
                ),
                str(uuid7()),
            )
            if days := g.consent.captured_days_ago:  # an old consent: TTL lapses as time passes
                back = timedelta(days=days)
                self.db.execute(
                    "UPDATE consent.record SET granted_at = granted_at - %s WHERE consent_id = %s",
                    (back, record.consent_id),
                )
                self.db.execute(
                    "UPDATE consent.purpose_grant SET changed_at = changed_at - %s"
                    " WHERE consent_id = %s",
                    (back, record.consent_id),
                )
            valid = record.valid_p1 and not days
            sets.append("consent_id = %s")
            params.append(record.consent_id)
        if g.state is not None:
            sets += ["fsm_state = %s", "status = %s"]
            params += [g.state.value, "paused" if g.paused_from else "active"]
        if g.paused_from is not None:
            sets.append("frame_stack = %s")
            params.append(Jsonb([{"state": g.paused_from.value}]))
        if g.pins:
            sets.append("pins = pins || %s")
            params.append(Jsonb(g.pins))
        if sets:
            self.db.execute(
                f"UPDATE conv.session SET {', '.join(sets)} WHERE session_id = %s",  # noqa: S608
                (*params, self.session_id),
            )
        return valid

    # --- turns -------------------------------------------------------------------------------
    def headers(self, key: UUID) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Idempotency-Key": str(key)}

    async def send(self, index: int, turn: Turn, key: UUID) -> Sent:
        url = f"/v1/sessions/{self.session_id}"
        if turn.delete:
            response = await self.api.delete(url, headers=self.headers(key))
        else:
            body = (
                {"text": turn.text}
                if turn.text is not None
                else {"action": turn.action.model_dump()}
            )  # type: ignore[union-attr]
            response = await self.api.post(f"{url}/turns", json=body, headers=self.headers(key))
        return Sent(index, response.status_code, response.content, customer_text=turn.text)

    async def play(self, index: int, turn: Turn, t: Transcript) -> None:
        if turn.kill_switch is not None:
            await self.kill_switch(index, turn)
        for route in ROUTES:
            if replies := turn.replies(route):
                scripted = await self.stubs.post(
                    "/__script",
                    json={"session_id": str(self.session_id), "route": route, "responses": replies},
                )
                scripted.raise_for_status()
        logged = len(self.read_logs())
        graph = self.runtime.graph
        if turn.checkpoint_lost:
            self.runtime.graph = build_graph(NoCheckpoint(graph.checkpointer.conn))  # type: ignore[union-attr]
        try:
            if turn.retry:
                if self.last is None:
                    raise AssertionError("retry needs an earlier request")
                previous, request, key = self.last
                sent = await self.send(index, request, key)
                sent.replay_of = previous
                t.sent.append(sent)
            elif turn.concurrent:
                pair = await asyncio.gather(
                    self.send(index, turn, uuid7()), self.send(index, turn, uuid7())
                )
                t.sent.extend(pair)
                statuses = sorted(s.status for s in pair)
                if statuses != [200, 409] or 409 not in [s.status for s in pair]:
                    self.fail(index, f"concurrent statuses {statuses}, want [200, 409]")
                busy = next((s for s in pair if s.status == 409), None)
                if busy and json.loads(busy.body).get("code") != "SESSION_BUSY":
                    self.fail(index, "the concurrent 409 is not SESSION_BUSY")
            else:
                key = uuid7()
                t.sent.append(await self.send(index, turn, key))
                self.last = (len(t.sent) - 1, turn, key)
        finally:
            self.runtime.graph = graph
        if turn.expect.hydrated and "checkpoint lags conv" not in self.read_logs()[logged:]:
            self.fail(index, "expected the session to be hydrated from conv")
        self.snapshot(t)

    async def kill_switch(self, index: int, turn: Turn) -> None:
        switch = turn.kill_switch
        assert switch is not None
        if self.system_seq is None:
            self.system_seq = self.one(
                "SELECT coalesce(max(seq), 0) FROM audit.audit_event WHERE session_id = %s", SYSTEM
            )
        made = await self.api.post(
            "/internal/kill-switches",
            json={"kind": switch.kind, "target": switch.target, "reason_code": switch.reason_code},
            headers={"Authorization": f"Bearer {self.settings.ops_api_key.get_secret_value()}"},
        )
        if made.status_code != 201:
            self.fail(index, f"kill switch refused: {made.status_code}")
            return
        self.switches.append(made.json()["id"])

    def snapshot(self, t: Transcript) -> None:
        """What only conv shows between turns (an erasure deletes it): redacted text and slots."""
        rows = self.db.execute(
            "SELECT redacted FROM conv.turn WHERE session_id = %s", (self.session_id,)
        ).fetchall()
        t.redacted += [r[0] for r in rows if r[0] not in t.redacted]
        consented = t.consent_valid_from_start or self.one(
            "SELECT count(*) FROM audit.audit_event WHERE session_id = %s"
            " AND event_type = 'CONSENT_CAPTURED' AND header->'purposes' ? 'P1'",
            self.session_id,
        )
        if not consented:
            t.slots_without_consent += self.one(
                "SELECT count(*) FROM conv.slot_value WHERE session_id = %s", self.session_id
            )

    def read_logs(self) -> str:
        return "".join(p.read_text() for p in sorted(self.log_dir.glob("*.log")))

    # --- evidence ----------------------------------------------------------------------------
    async def collect(self, t: Transcript) -> None:
        with self.db.cursor() as cur:
            rows = cur.execute(
                "SELECT event_id, session_id, seq, event_type, occurred_at, fsm_state, pins,"
                " header, payload_enc, key_ref, prev_hash, hash FROM audit.audit_event"
                " WHERE session_id = %s ORDER BY seq",
                (self.session_id,),
            ).fetchall()
        for row in rows:
            event = AuditEvent(*row)
            try:
                payload = decrypt_payload(self.runtime.keys, event)
            except KeyDestroyed:
                payload = None
            t.events.append(Event(event.seq, event.event_type, event.header, event.pins, payload))
        t.switched_bundles = {
            r[0]
            for r in self.db.execute(
                "SELECT header->>'target' FROM audit.audit_event WHERE session_id = %s"
                " AND event_type = 'KILL_SWITCH' AND header->>'target_kind' = 'prompt_bundle'"
                " AND (header->>'active')::boolean",
                (SYSTEM,),
            ).fetchall()
        }
        compliance = {
            "Authorization": f"Bearer {self.settings.compliance_api_key.get_secret_value()}"
        }
        verified = await self.api.get(
            f"/internal/audit/sessions/{self.session_id}/verify", headers=compliance
        )
        t.chain_ok, t.chain_checked = verified.json()["ok"], verified.json()["checked"]
        advisor = {"Authorization": f"Bearer {self.settings.advisor_api_key.get_secret_value()}"}
        for e in t.events:
            if e.event_type == "HANDOFF":
                found = await self.api.get(
                    f"/internal/handoffs/{e.header['handoff_id']}", headers=advisor
                )
                if found.status_code == 200:  # an erasure removes it
                    t.briefings.append(found.json()["briefing"])
        t.logs = self.read_logs()
        t.erased = self.erased()
        for s in t.sent:
            released = s.released
            if released is not None and released["state"] == "S3":
                for d in released["message"]["disclosures"]:
                    registered = await self.runtime.domain.get_disclosure_set(
                        d["uin"], self.conversation.channel, self.conversation.locale
                    )
                    t.registry[d["uin"]] = registered.set_sha256
        t.evidence = await self.evidence(t)

    def erased(self) -> bool:
        session = self.one(
            "SELECT count(*) FROM conv.session WHERE session_id = %s", self.session_id
        )
        checkpoints = self.one(
            "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = %s", str(self.session_id)
        )
        handled = self.db.execute(
            "SELECT destroy_after IS NOT NULL OR destroyed_at IS NOT NULL"
            " FROM keyvault.subject_key WHERE key_ref = %s",
            (self.key_ref,),
        ).fetchone()
        return session == 0 and checkpoints == 0 and bool(handled and handled[0])

    async def evidence(self, t: Transcript) -> dict[str, str]:
        """The text of every chunk a RETRIEVAL handed out, from Qdrant (the payload holds ids)."""
        chunk_ids = sorted(
            {c for e in t.events if e.event_type == "RETRIEVAL" and e.payload
             for c in e.payload["handle_map"].values()}
        )  # fmt: skip
        if not chunk_ids:
            return {}
        qdrant = AsyncQdrantClient(url=self.settings.qdrant_url)
        found: dict[str, str] = {}
        try:
            for collection in (await qdrant.get_collections()).collections:
                points, _ = await qdrant.scroll(
                    collection.name,
                    scroll_filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="chunk_id", match=models.MatchAny(any=chunk_ids)
                            )
                        ]
                    ),
                    limit=len(chunk_ids) * 4,
                )
                found |= {p.payload["chunk_id"]: p.payload["text"] for p in points if p.payload}
        finally:
            await qdrant.close()
        return found

    # --- expectations ------------------------------------------------------------------------
    def expectations(self, t: Transcript) -> None:
        turns = self.conversation.turns
        for s in t.sent:
            expect = turns[s.turn].expect
            if turns[s.turn].concurrent:
                continue  # checked as a pair in play()
            if s.status != expect.status:
                self.fail(s.turn, f"status {s.status}, want {expect.status}: {s.body[:200]!r}")
                continue
            released = s.released
            if released is None:
                continue
            events = t.turn_events(released)
            types = {e.event_type for e in events}
            if expect.state is not None and released["state"] != expect.state.value:
                self.fail(s.turn, f"state {released['state']}, want {expect.state.value}")
            ids = [i.removeprefix("template:") for i in part_ids(released)]
            if expect.templates is not None and ids != expect.templates:
                self.fail(s.turn, f"templates {ids}, want {expect.templates}")
            if missing := sorted({e.value for e in expect.events} - types):
                self.fail(s.turn, f"audit events missing: {missing}")
            if expect.handoff is not None:
                reasons = [e.header["reason_code"] for e in events if e.event_type == "HANDOFF"]
                if reasons != [expect.handoff]:
                    self.fail(s.turn, f"handoff {reasons}, want {expect.handoff}")
            cited = bool(released["message"]["citations"])
            if expect.citations is not None and cited != expect.citations:
                self.fail(s.turn, f"citations present: {cited}, want {expect.citations}")
            if expect.abstained is not None:
                abstained = [e.header["abstained"] for e in events if e.event_type == "RETRIEVAL"]
                if abstained != [expect.abstained]:
                    self.fail(s.turn, f"abstained {abstained}, want {expect.abstained}")
            if expect.disclosures is not None:
                uins = [d["uin"] for d in released["message"]["disclosures"]]
                if uins != expect.disclosures:
                    self.fail(s.turn, f"disclosures {uins}, want {expect.disclosures}")
            if expect.erased is not None:
                self.expect_erased(s.turn, expect.erased)

    def expect_erased(self, index: int, fate: str) -> None:
        if not self.erased():
            self.fail(index, "live rows or the checkpoint survived the erasure")
        destroyed = self.one(
            "SELECT destroyed_at IS NOT NULL FROM keyvault.subject_key WHERE key_ref = %s",
            self.key_ref,
        )
        if destroyed != (fate == "destroyed"):
            self.fail(index, f"the subject key was not {fate}")

    # --- cleanup -----------------------------------------------------------------------------
    async def close(self) -> None:
        """Delete everything the conversation created, so the dev database is left as found."""
        try:
            if self.session_id is not None:
                await self.stubs.delete(f"/__script/{self.session_id}")
                sid, db = self.session_id, self.db
                db.execute(
                    "DELETE FROM conv.disclosure_ack WHERE rec_id IN"
                    " (SELECT rec_id FROM conv.recommendation WHERE session_id = %s)",
                    (sid,),
                )
                for table in CONV_TABLES:
                    db.execute(f"DELETE FROM conv.{table} WHERE session_id = %s", (sid,))  # noqa: S608
                for table in CHECKPOINT_TABLES:
                    db.execute(f"DELETE FROM langgraph.{table} WHERE thread_id = %s", (str(sid),))  # noqa: S608
                db.execute("DELETE FROM audit.audit_event WHERE session_id = %s", (sid,))
                consents = [
                    r[0]
                    for r in db.execute(
                        "SELECT consent_id FROM consent.record WHERE session_id = %s", (sid,)
                    ).fetchall()
                ]
                db.execute(
                    "DELETE FROM consent.purpose_grant WHERE consent_id = ANY(%s)", (consents,)
                )
                db.execute("DELETE FROM consent.record WHERE consent_id = ANY(%s)", (consents,))
                db.execute("DELETE FROM keyvault.subject_key WHERE key_ref = %s", (self.key_ref,))
            if self.switches:
                self.db.execute("DELETE FROM conv.kill_switch WHERE id = ANY(%s)", (self.switches,))
                self.db.execute(
                    "DELETE FROM audit.audit_event WHERE session_id = %s AND seq > %s",
                    (SYSTEM, self.system_seq),
                )
        finally:
            await self.api.aclose()
            await self.stubs.aclose()
            self.db.close()


CASES = load_conversations()


@pytest.mark.parametrize("conversation", CASES, ids=[c.id for c in CASES])
@pytest.mark.usefixtures("restore_logging")
async def test_golden_conversation(conversation: Conversation, tmp_path: Path) -> None:
    # SS_* come from `make eval` (the dev database, the infra/.env tokens); a developer's
    # orchestrator/.env never redirects a golden run.
    settings = Settings(_env_file=None, log_level="INFO", log_dir=tmp_path)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        run = Play(app, settings, conversation, tmp_path)
        try:
            transcript = await run.open()
            for index, turn in enumerate(conversation.turns):
                await run.play(index, turn, transcript)
            await run.collect(transcript)
            run.expectations(transcript)
        finally:
            await run.close()
    failures = run.failures + [f"{conversation.id}: {f}" for f in check(transcript)]
    assert not failures, "\n".join(failures)
