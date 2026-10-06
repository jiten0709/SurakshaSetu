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
import gc
import hashlib
import json
import os
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from harness import (
    FIXTURE_UIN,
    ROUTES,
    Conversation,
    Event,
    Sent,
    Transcript,
    Turn,
    check,
    load_conversations,
    part_ids,
    release_locale,
)
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.types.json import Jsonb
from qdrant_client import AsyncQdrantClient, models

from surakshasetu.api.app import create_app
from surakshasetu.audit.chain import AuditEvent, decrypt_payload
from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.crypto.keys import KeyDestroyed
from surakshasetu.domain.client import DomainError
from surakshasetu.domain.models import ConsentRecordCreate, PurposeGrant
from surakshasetu.graph import handlers, side_query
from surakshasetu.graph.nodes import build_graph
from surakshasetu.graph.runtime import Runtime
from surakshasetu.handoff import intake
from surakshasetu.jobs import timers
from surakshasetu.retrieval.service import RetrievalUnavailable
from surakshasetu.uuid7 import uuid7

pytestmark = [pytest.mark.golden, pytest.mark.asyncio]

STUBS = os.environ.get("SS_TEST_STUBS_URL", "http://127.0.0.1:8090")
SYSTEM = UUID(int=0)
PURPOSES = {"P1": "P1_NEEDS_RECO", "P2": "P2_ADVISOR_CONTACT", "P3": "P3_MARKETING"}
CONV_TABLES = ("recommendation", "handoff", "slot_value", "turn", "session")
# Step 21: the tampered registry version a turn's `registry_tamper` adds (and removes).
TAMPERED = "9999.12.1"
FIXTURE_SET = (
    "DISC-GLOBAL-SOLICIT-01",
    "DISC-GLOBAL-QUOTE-02",
    "DISC-GLOBAL-S45-03",
    "DISC-GLOBAL-FREELOOK-04",
    "DISC-GLOBAL-TAX-05",
)
CHECKPOINT_TABLES = ("checkpoint_writes", "checkpoint_blobs", "checkpoints")
# OmniRoute sidelines a failed target for about 3 s (infra/omniroute/HARDENING.md), and every route
# shares the stub connection: after a turn that scripts a failure, the next conversation's model
# calls would fail closed. Step 22 waits it out.
GATEWAY_COOLDOWN_S = 3.5
IST = ZoneInfo("Asia/Kolkata")


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
        self.form: dict[str, Any] | None = None  # the last consent form released (Step 18)
        self.bumped: list[str] = []  # notice versions this conversation added
        self.quick: list[dict[str, Any]] = []  # the last quick replies released (Step 21)
        self.fixture = False  # FIXTURE_UIN was added to the catalog (Step 21)

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
            products={p.uin: p.name for p in products},
        )

    async def given(self, subject_ref: UUID) -> bool:
        """Apply the `given` block as superuser. True when a valid P1 exists from the start."""
        g, sets, params = self.conversation.given, [], []
        valid = False
        if g.fixture_product:
            self.add_fixture()
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

    def add_fixture(self) -> None:
        """FIXTURE_UIN in the dev catalog (Step 21): a TERM plan in force since yesterday (IST),
        launched, with the global disclosures as its set (hashed as the registry hashes) and a CIS
        and policy wording; absent from the DUMMY rate table, so its option is RATING_UNAVAILABLE.
        Seed rows are never touched; close() deletes these."""
        self.fixture = True
        since = datetime.now(IST).date() - timedelta(days=1)
        self.db.execute(
            "INSERT INTO catalog.product (uin, name, category, status, entry_age_min,"
            " entry_age_max, maturity_age_max, sa_min_inr, sa_max_inr, term_years, ppt_options,"
            " payout_options, rider_uins, effective_from, launch_enabled, is_dummy) VALUES"
            " (%s, 'Golden Fixture Term', 'TERM', 'in_force', 18, 65, 85, 2500000, 100000000,"
            " '[10,41)', '{regular}', '{lumpsum}', '{}', %s, true, true)",
            (FIXTURE_UIN, since),
        )
        for kind in ("CIS", "POLICY_WORDING"):
            self.db.execute(
                "INSERT INTO catalog.product_document (uin, kind, version, language, uri, sha256,"
                " is_dummy) VALUES (%s, %s, 'v1', 'en-IN', %s, %s, true)",
                (
                    FIXTURE_UIN,
                    kind,
                    f"golden://{FIXTURE_UIN}/{kind}",
                    hashlib.sha256(kind.encode()).digest(),
                ),
            )
        for channel in ("web", "app"):
            for language in ("en-IN", "hi-IN"):
                self.insert_set(FIXTURE_UIN, channel, language, "2026.09.1", list(FIXTURE_SET))

    def insert_set(
        self,
        uin: str,
        channel: str,
        language: str,
        version: str,
        ids: list[str],
        *,
        bad: bool = False,
    ) -> None:
        hashes = dict(
            self.db.execute(
                "SELECT disclosure_id, encode(body_sha256, 'hex') FROM catalog.disclosure"
                " WHERE language = %s AND disclosure_id = ANY(%s)",
                (language, ids),
            ).fetchall()
        )
        digest = (
            bytes(32)
            if bad
            else bytes.fromhex(
                sha256_hex(
                    {
                        "registry_version": version,
                        "uin": uin,
                        "channel": channel,
                        "language": language,
                        "items": [{"disclosure_id": i, "body_sha256": hashes[i]} for i in ids],
                    }
                )
            )
        )
        self.db.execute(
            "INSERT INTO catalog.disclosure_set (uin, channel, language, registry_version,"
            " disclosure_ids, set_sha256) VALUES (%s, %s, %s, %s, %s, %s)",
            (uin, channel, language, version, ids, digest),
        )

    def tamper(self, uin: str) -> None:
        """A newer set for the conversation's channel and language whose stored hash is wrong:
        the registry refuses to serve it (REGISTRY_INTEGRITY)."""
        c = self.conversation
        ids = self.one(
            "SELECT disclosure_ids FROM catalog.disclosure_set WHERE uin = %s AND channel = %s"
            " AND language = %s ORDER BY registry_version DESC LIMIT 1",
            uin,
            c.channel,
            c.locale,
        )
        self.insert_set(uin, c.channel, c.locale, TAMPERED, list(ids), bad=True)

    def untamper(self) -> None:
        self.db.execute(
            "DELETE FROM catalog.disclosure_set WHERE registry_version = %s", (TAMPERED,)
        )

    # --- turns -------------------------------------------------------------------------------
    def headers(self, key: UUID) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "Idempotency-Key": str(key)}

    async def send(self, index: int, turn: Turn, key: UUID) -> Sent:
        url = f"/v1/sessions/{self.session_id}"
        if turn.delete:
            response = await self.api.delete(url, headers=self.headers(key))
        else:
            body = {"text": turn.text} if turn.text is not None else {"action": self.action(turn)}
            response = await self.api.post(f"{url}/turns", json=body, headers=self.headers(key))
        sent = Sent(index, response.status_code, response.content, customer_text=turn.text)
        if (released := sent.released) is not None and released["message"].get("form"):
            self.form = released["message"]["form"]
        if released is not None:
            self.quick = released["message"]["quick_replies"]
        return sent

    def action(self, turn: Turn) -> dict[str, Any]:
        """The turn's action. A CONSENT_SUBMIT names the notice of the last form released, as a
        client would, unless the conversation gives one (a mismatch case)."""
        action = turn.action.model_dump()  # type: ignore[union-attr]
        if action["type"] == "CONSENT_SUBMIT" and self.form is not None:
            shown = {k: self.form[k] for k in ("notice_version", "notice_sha256")}
            action["payload"] = shown | action["payload"]
        if action["type"] == "DISCLOSURE_ACK":  # Step 21: as the client sends the ack it was shown
            offered = next(
                (
                    q["action"]["payload"]
                    for q in self.quick
                    if q["action"]["type"] == "DISCLOSURE_ACK"
                    and q["action"]["payload"]["uin"] == action["payload"].get("uin")
                ),
                {},
            )
            action["payload"] = offered | action["payload"]
        return action

    def bump_notice(self) -> None:
        """A newer notice for the conversation's language, in force from today (IST): the
        session's consent becomes NOTICE_SUPERSEDED. Removed by close()."""
        version = f"golden-{uuid7().hex[-8:]}-{self.conversation.locale[:2]}"
        body = f"DUMMY: golden notice {version}, superseding the seed notice."
        digest = hashlib.sha256(unicodedata.normalize("NFC", body).encode()).digest()
        self.db.execute(
            "INSERT INTO consent.notice_version (notice_version, language, body, body_sha256,"
            " approved_by, effective_from, is_dummy) VALUES (%s, %s, %s, %s, %s, %s, true)",
            (version, self.conversation.locale, body, digest, "GOLDEN", datetime.now(IST).date()),
        )
        self.bumped.append(version)

    async def play(self, index: int, turn: Turn, t: Transcript) -> None:
        if turn.kill_switch is not None:
            await self.kill_switch(index, turn)
        if turn.notice_bump:
            self.bump_notice()
        if turn.registry_tamper is not None:
            self.tamper(turn.registry_tamper)
        if turn.journey_down:
            failed = await self.stubs.post(
                "/journey/__fail", json={"session_id": str(self.session_id), "status": 503}
            )
            failed.raise_for_status()
        clock = handlers.now
        if turn.days_later:
            ahead = timedelta(days=turn.days_later)
            handlers.now = lambda: datetime.now(UTC) + ahead  # type: ignore[assignment]
        restore = self.take_down(turn)
        before = len(t.sent)
        for route in ROUTES:
            if replies := turn.replies(route):
                scripted = await self.stubs.post(
                    "/__script",
                    json={"session_id": str(self.session_id), "route": route, "responses": replies},
                )
                scripted.raise_for_status()
        logged = self.log_sizes()
        graph = self.runtime.graph
        if turn.checkpoint_lost:
            self.runtime.graph = build_graph(NoCheckpoint(graph.checkpointer.conn))  # type: ignore[union-attr]
        try:
            if turn.timer is not None:
                await self.timer(index, turn, t)
            elif turn.retry:
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
            handlers.now = clock  # type: ignore[assignment]
            restore()
            if any(
                isinstance(r, dict) and r.get("status", 200) >= 500
                for route in ROUTES
                for r in turn.replies(route)
            ):
                await asyncio.sleep(GATEWAY_COOLDOWN_S)
            if turn.registry_tamper is not None:
                self.untamper()
        if turn.expect.intake is not None:
            await self.check_intake(index, turn.expect.intake, t)
        if turn.expect.hydrated and "checkpoint lags conv" not in self.logs_since(logged):
            self.fail(index, "expected the session to be hydrated from conv")
        counters = self.one(
            "SELECT counters FROM conv.session WHERE session_id = %s", self.session_id
        )
        slot_rows = self.one(
            "SELECT count(*) FROM conv.slot_value WHERE session_id = %s", self.session_id
        )
        slots = dict(
            self.db.execute(
                "SELECT DISTINCT ON (slot) slot, status FROM conv.slot_value WHERE session_id = %s"
                " ORDER BY slot, created_at DESC, slot_value_id DESC",
                (self.session_id,),
            ).fetchall()
        )
        frames = self.one(
            "SELECT frame_stack FROM conv.session WHERE session_id = %s", self.session_id
        )
        for sent in t.sent[before:]:
            sent.counters, sent.slot_rows, sent.slots = counters, slot_rows, slots
            sent.frames = frames
        self.snapshot(t)

    def take_down(self, turn: Turn) -> Any:
        """Step 22: the dependencies this turn finds down (the in-process orchestrator's domain
        client for the named operations, or retrieval). Returns the undo."""
        domain, retrieval = self.runtime.domain, self.runtime.retrieval
        call, retrieve = domain._call, retrieval.retrieve if retrieval else None
        down = set(turn.domain_down)
        if down:

            async def failing(op: str, *args: Any, **kwargs: Any) -> Any:
                if op in down:
                    raise DomainError("UNAVAILABLE", None)
                return await call(op, *args, **kwargs)

            domain._call = failing  # type: ignore[method-assign]
        if turn.retrieval_down and retrieval is not None:

            async def unavailable(*args: Any, **kwargs: Any) -> Any:
                raise RetrievalUnavailable("QDRANT_UNAVAILABLE")

            retrieval.retrieve = unavailable  # type: ignore[method-assign]

        def undo() -> None:
            domain._call = call  # type: ignore[method-assign]
            if retrieval is not None and retrieve is not None:
                retrieval.retrieve = retrieve  # type: ignore[method-assign]

        return undo

    async def timer(self, index: int, turn: Turn, t: Transcript) -> None:
        """Step 22: the timer job looks `turn.timer` minutes from now, at this session only. Its
        released pause is recorded like a turn's; nothing released means the session was left
        alone (before consent it is passive, V3)."""
        later = datetime.now(UTC) + timedelta(minutes=cast(int, turn.timer))
        bodies = await timers.run_once(self.runtime, later, [cast(UUID, self.session_id)])
        t.sent.extend(Sent(index, 200, body) for body in bodies)
        fired = bool(bodies)
        want = turn.expect.timer_fired
        if want is not None and fired != want:
            self.fail(index, f"timer fired: {fired}, want {want}")
        state = self.one(
            "SELECT fsm_state FROM conv.session WHERE session_id = %s", self.session_id
        )
        if not fired and turn.expect.state is not None and state != turn.expect.state.value:
            self.fail(index, f"state {state} after the timer, want {turn.expect.state.value}")

    async def check_intake(self, index: int, want: bool, t: Transcript) -> None:
        """The stub journey verified a signed intake for this session (Step 21), and it is the one
        the orchestrator's HANDOFF recorded (checked again in expectations, from the audit)."""
        found = await self.stubs.get(f"/journey/__intake/{self.session_id}")
        got = found.status_code == 200
        if got != want:
            self.fail(index, f"journey intake received: {got}, want {want}")
            return
        if got:
            signed = found.json()["payload"]
            public = intake.public_key_b64(
                intake.signing_key(self.settings.intake_signing_key_b64.get_secret_value())
            )
            if not intake.verify(signed, public) or signed["session_id"] != str(self.session_id):
                self.fail(index, "the journey's intake does not verify for this session")
            t.intakes.append(signed)

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

    def log_sizes(self) -> dict[Path, int]:
        return {p: len(p.read_text()) for p in self.log_dir.glob("*.log")}

    def logs_since(self, sizes: dict[Path, int]) -> str:
        """What each subsystem file gained since `sizes`. Offsets are per file: a line lands in
        its package's file, and the files are not appended in name order."""
        return "".join(p.read_text()[sizes.get(p, 0) :] for p in sorted(self.log_dir.glob("*.log")))

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
            t.events.append(
                Event(
                    event.seq, event.event_type, event.header, event.pins, payload, event.fsm_state
                )
            )
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
                release = next(
                    (e for e in t.turn_events(released) if e.event_type == "RESPONSE_RELEASED"),
                    None,
                )
                locale = release_locale(t, release)
                for d in released["message"]["disclosures"]:
                    registered = await self.runtime.domain.get_disclosure_set(
                        d["uin"], self.conversation.channel, locale
                    )
                    t.registry_sets[(d["uin"], locale)] = registered.set_sha256
                    if locale == self.conversation.locale:
                        t.registry[d["uin"]] = registered.set_sha256
        t.evidence = await self.evidence(t)
        await self.consent_texts(t)

    async def consent_texts(self, t: Transcript) -> None:
        """The Consent Service's notices and the registry's single disclosures, as the services
        hold them, for every notice part, registry part and form released
        (harness.check_consent_prompt)."""
        versions: set[str] = set()
        for s in t.sent:
            if (released := s.released) is not None:
                versions |= {i[7:] for i in part_ids(released) if i.startswith("notice:")}
                if form := released["message"].get("form"):
                    versions.add(form["notice_version"])
        for version in sorted(versions):
            try:
                notice = await self.runtime.domain.get_consent_notice(version)
            except DomainError:
                continue  # not held: the check reports it
            t.notices[version] = (notice.body, notice.body_sha256)
        for language in ("en-IN", "hi-IN"):
            for disclosure_id in (
                "DISC-GLOBAL-AI-06",
                "DISC-GLOBAL-QUOTE-02",
                "DISC-GLOBAL-TAX-05",
            ):
                found = await self.runtime.domain.get_disclosure(disclosure_id, language)
                t.registry_bodies.add(found.body)
            # Step 22: the approved privacy FAQ's answers, as the faq: parts must carry them
            t.faq_answers |= {
                e.answer for e in side_query.privacy_faq(language, self.settings.env).entries
            }

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
                # the text, and (Step 21) the citation label a rendered [Source: ...] shows
                found |= {
                    p.payload["chunk_id"]: f"{p.payload['text']} {p.payload['citation_label']}"
                    for p in points
                    if p.payload
                }
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
            ids = [
                i.removeprefix("template:") for i in part_ids(released) if i.startswith("template:")
            ]
            if expect.templates is not None and ids != expect.templates:
                self.fail(s.turn, f"templates {ids}, want {expect.templates}")
            if missing := sorted({e.value for e in expect.events} - types):
                self.fail(s.turn, f"audit events missing: {missing}")
            if present := sorted({e.value for e in expect.absent} & types):
                self.fail(s.turn, f"audit events present: {present}")
            if expect.consent is not None:
                self.expect_consent(s.turn, expect.consent, events)
            if expect.counters is not None:
                counters = {k: (s.counters or {}).get(k, 0) for k in expect.counters}
                if counters != expect.counters:
                    self.fail(s.turn, f"counters {counters}, want {expect.counters}")
            if expect.slot_rows is not None and s.slot_rows != expect.slot_rows:
                self.fail(s.turn, f"slot rows {s.slot_rows}, want {expect.slot_rows}")
            form = released["message"].get("form") is not None
            if expect.form is not None and form != expect.form:
                self.fail(s.turn, f"consent form present: {form}, want {expect.form}")
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
            self.expect_step19(s, expect, released, events)
            if expect.actions is not None:
                got = [q["action"]["type"] for q in released["message"]["quick_replies"]]
                if got != expect.actions:
                    self.fail(s.turn, f"quick replies {got}, want {expect.actions}")
            self.expect_step22(s, expect, events)
            if expect.intake:
                sent = [
                    e.payload["intake"] for e in events
                    if e.event_type == "HANDOFF" and e.payload and "intake_ref" in e.payload
                ]  # fmt: skip
                if sent != t.intakes[-1:]:
                    self.fail(s.turn, "the HANDOFF intake is not the one the journey verified")

    def expect_step22(self, s: Sent, expect: Any, events: list[Event]) -> None:
        """The RESPONSE_RELEASED header's language, FAQ outcome and objection (Step 22)."""
        release = next((e for e in events if e.event_type == "RESPONSE_RELEASED"), None)
        header = release.header if release else {}
        for name in ("language", "faq", "objection", "objection_response"):
            want = getattr(expect, name)
            if want is not None and header.get(name) != want:
                self.fail(s.turn, f"RESPONSE_RELEASED {name} {header.get(name)}, want {want}")
        chunks = [c for e in events if e.event_type == "RETRIEVAL" for c in e.header["chunk_ids"]]
        for prefix in expect.retrieved:
            if not any(c.startswith(prefix) for c in chunks):
                self.fail(s.turn, f"no {prefix}* chunk retrieved: {chunks}")
        for prefix in expect.not_retrieved:
            if found := [c for c in chunks if c.startswith(prefix)]:
                self.fail(s.turn, f"{prefix}* chunks retrieved: {found}")

    def expect_step19(
        self, s: Sent, expect: Any, released: dict[str, Any], events: list[Event]
    ) -> None:
        """The engine's decision, the slot rows' statuses, every part id, the released text, and
        the fsm row that decided the turn."""
        text = released["message"]["text"]
        if expect.engine is not None:
            want = expect.engine
            found = [
                e for e in events
                if e.event_type == "ENGINE_DECISION" and e.header.get("service") == want.service
            ]  # fmt: skip
            # Step 22: once the subject key is destroyed (a minor's erasure), the payloads are
            # unreadable and only the decision itself (its header) can be checked.
            decisions = [e.payload for e in found if e.payload is not None]
            results = [d.get("result", {}) for d in decisions]
            if not found:
                self.fail(s.turn, f"no {want.service} ENGINE_DECISION in the turn")
            for name in ("outcome", "flags", "reason_codes"):
                value = getattr(want, name)
                if value is not None and results and all(r.get(name) != value for r in results):
                    got = [r.get(name) for r in results]
                    self.fail(s.turn, f"{want.service} {name} {got}, want {value}")
            for name, value in want.result.items():  # Step 20
                if results and all(r.get(name) != value for r in results):
                    got = [r.get(name) for r in results]
                    self.fail(s.turn, f"{want.service} {name} {got}, want {value}")
        if expect.slots is not None:
            got = {k: s.slots.get(k) for k in expect.slots}
            if got != expect.slots:
                self.fail(s.turn, f"slots {got}, want {expect.slots}")
        if expect.parts is not None and part_ids(released) != expect.parts:
            self.fail(s.turn, f"parts {part_ids(released)}, want {expect.parts}")
        for wanted in expect.contains:
            if wanted not in text:
                self.fail(s.turn, f"text lacks {wanted!r}")
        for unwanted in expect.lacks:
            if unwanted in text:
                self.fail(s.turn, f"text has {unwanted!r}")
        moves = [e.header for e in events if e.event_type == "STATE_TRANSITION"]
        if expect.row is not None and [m["trigger"] for m in moves] != [expect.row]:
            self.fail(s.turn, f"rows {[m['trigger'] for m in moves]}, want {expect.row}")
        if expect.reason is not None and [m.get("reason_code") for m in moves] != [expect.reason]:
            self.fail(
                s.turn, f"reasons {[m.get('reason_code') for m in moves]}, want {expect.reason}"
            )

    def expect_consent(self, index: int, expect: Any, events: list[Event]) -> None:
        captured = [e.header for e in events if e.event_type == "CONSENT_CAPTURED"]
        if not captured:
            self.fail(index, "no CONSENT_CAPTURED in the turn")
            return
        header = captured[-1]
        for field in ("method", "purposes", "language"):
            want = getattr(expect, field)
            if want is not None and header.get(field) != want:
                self.fail(index, f"CONSENT_CAPTURED {field} {header.get(field)}, want {want}")

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
            if self.bumped:  # after the records that name them
                self.db.execute(
                    "DELETE FROM consent.notice_version WHERE notice_version = ANY(%s)",
                    (self.bumped,),
                )
            if self.session_id is not None:
                await self.stubs.delete(f"/journey/__intake/{self.session_id}")
            if self.switches:
                self.db.execute("DELETE FROM conv.kill_switch WHERE id = ANY(%s)", (self.switches,))
                self.db.execute(
                    "DELETE FROM audit.audit_event WHERE session_id = %s AND seq > %s",
                    (SYSTEM, self.system_seq),
                )
        finally:
            # Step 21: whatever failed above, no tampered set or fixture product outlives the
            # conversation (a fixture left behind would be ranked for every later session).
            self.untamper()
            if self.fixture:
                for table in ("product_document", "disclosure_set", "product"):
                    self.db.execute(f"DELETE FROM catalog.{table} WHERE uin = %s", (FIXTURE_UIN,))  # noqa: S608
            await self.api.aclose()
            await self.stubs.aclose()
            self.db.close()


CASES = load_conversations()


@pytest.mark.parametrize("conversation", CASES, ids=[c.id for c in CASES])
@pytest.mark.usefixtures("restore_logging")
async def test_golden_conversation(conversation: Conversation, tmp_path: Path) -> None:
    # SS_* come from `make eval` (the dev database, the infra/.env tokens); a developer's
    # orchestrator/.env never redirects a golden run.
    # A conversation replays a customer's turns at machine speed: S2's full discovery alone is 26
    # turns inside a minute, over the production limit of 20 a minute per subject. The limiter
    # itself is covered by the runtime tests; here it only needs to let the conversation through.
    settings = Settings(
        _env_file=None, log_level="INFO", log_dir=tmp_path, rate_limits={60: 60, 3600: 600}
    )
    # Step 22: a long run (176 conversations in one process) had full collections of 115-200 ms
    # mid-turn, which stall the in-process orchestrator past a domain call's 150 ms budget. Collect
    # between conversations and freeze what survives (module state), so a collection during a turn
    # scans only that conversation's objects.
    gc.collect()
    gc.freeze()
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
