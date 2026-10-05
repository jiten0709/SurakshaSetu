"""The conversation runtime: the dependencies a turn needs, session creation and authentication,
the turn gate (single-writer lock and rate limit, taken before the graph runs), the erasure that
follows an erasure turn's commit (as erasure_rw, never app_rw), and the internal operations (audit
verification, kill switches, the advisor hand-off queue).

Storage stays on sync psycopg (Steps 4, 10 and 14 are sync): a connection is taken from the pool
off the event loop, and each statement then blocks the loop for one round trip.
"""

import asyncio
import base64
import hashlib
import hmac
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from http import HTTPStatus
from typing import Any, Literal, Self, cast
from uuid import UUID

import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.graph.state import CompiledStateGraph
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException

from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.chain import SYSTEM_SESSION, VerifyResult
from surakshasetu.audit.events import EventType, KillSwitchHeader
from surakshasetu.compose.bundle import BundleError, activate, load_bundle
from surakshasetu.config import ConfigError, Settings
from surakshasetu.crypto.jcs import canonical_json
from surakshasetu.crypto.keys import SYSTEM_KEY_REF, KeyDestroyed, KeyService, LocalKeyService
from surakshasetu.domain.client import DomainClient, DomainError
from surakshasetu.domain.models import KillSwitch
from surakshasetu.gateway import Gateway, GatewayUnavailable
from surakshasetu.graph.gate import RedisGate
from surakshasetu.graph.handlers import data_erasure
from surakshasetu.graph.nodes import Turn, build_graph
from surakshasetu.graph.state import VersionPins
from surakshasetu.rails.output import LexiconPack, load_pack
from surakshasetu.retrieval.service import RetrievalService, load_snapshot_meta
from surakshasetu.store import conv as store
from surakshasetu.store.conv import Conn, HandoffRow, SessionRow
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger(__name__)

# Dependency failures before a commit: nothing was released, so the retry (same key) is safe.
UNAVAILABLE = (psycopg.Error, RedisError, DomainError, GatewayUnavailable, BundleError)
_NO_TOKEN = bytes(32)


class ProblemError(HTTPException):
    """An application/problem+json answer with the contract's `code` (api/app.py renders it).
    An HTTPException, so FastAPI passes it through unchanged even from inside body parsing."""

    def __init__(self, status: int, code: str | None = None) -> None:
        super().__init__(status_code=status)
        self.code = code or HTTPStatus(status).name


def token_sha256(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        pool: ConnectionPool[Conn],
        erasure: ConnectionPool[Conn],
        keys: KeyService,
        gate: RedisGate,
        domain: DomainClient,
        gateway: Gateway,
        pack: LexiconPack,
        graph: CompiledStateGraph[Any, Any],
        retrieval: RetrievalService | None = None,
    ) -> None:
        self.settings, self.pool, self.erasure, self.keys = settings, pool, erasure, keys
        self.gate, self.domain, self.gateway, self.pack = gate, domain, gateway, pack
        self.graph, self.retrieval = graph, retrieval

    @classmethod
    @asynccontextmanager
    async def open(cls, settings: Settings) -> AsyncIterator[Self]:
        if settings.pg_dsn_app is None or settings.redis_url is None:
            raise ConfigError("SS_PG_DSN_APP and SS_REDIS_URL are required to serve turns")
        dsn = settings.pg_dsn_app.get_secret_value()
        async with AsyncExitStack() as stack:
            pool: ConnectionPool[Conn] = stack.enter_context(
                ConnectionPool(
                    dsn,
                    min_size=1,
                    max_size=settings.pg_pool_max,
                    kwargs={"application_name": "orchestrator"},
                )
            )
            vault: ConnectionPool[Conn] = stack.enter_context(
                ConnectionPool(settings.pg_dsn_keyvault.get_secret_value(), min_size=1)
            )
            erasure: ConnectionPool[Conn] = stack.enter_context(
                ConnectionPool(
                    settings.pg_dsn_erasure.get_secret_value(),
                    min_size=1,
                    kwargs={"application_name": "orchestrator-erasure"},
                )
            )
            keys = LocalKeyService(vault, base64.b64decode(settings.kek_b64.get_secret_value()))
            # The checkpointer's own pool, configured as checkpointer_setup.py: autocommit,
            # dict rows, and the langgraph schema.
            saver_pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
                dsn,
                min_size=1,
                max_size=settings.pg_pool_max,
                open=False,
                kwargs={
                    "autocommit": True,
                    "row_factory": dict_row,
                    "prepare_threshold": 0,
                    "options": "-c search_path=langgraph",
                    "application_name": "orchestrator-checkpointer",
                },
            )
            await saver_pool.open()
            stack.push_async_callback(saver_pool.close)
            redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
            stack.push_async_callback(redis.aclose)
            domain = await stack.enter_async_context(DomainClient.from_settings(settings))
            gateway = await stack.enter_async_context(Gateway(settings))
            pack = load_pack(settings.output_lexicon)
            bundle = load_bundle(settings.prompt_bundle, env=settings.env)
            with pool.connection() as conn:  # commits on exit
                activate(conn, keys, bundle)  # CONFIG_RELEASE, once per bundle version
            graph = build_graph(AsyncPostgresSaver(saver_pool))
            # Step 21: S3's evidence. The client connects lazily: Qdrant down is a turn's
            # RetrievalUnavailable (an uncited card), never a refused start.
            qdrant = AsyncQdrantClient(url=settings.qdrant_url)
            stack.push_async_callback(qdrant.close)
            retrieval = RetrievalService(
                gateway, qdrant, partial(load_snapshot_meta, cast(Any, pool))
            )
            await data_erasure.sweep(pool, erasure, keys, domain, settings)
            logger.info("runtime ready: bundle %s, lexicon %s", bundle.version, pack.version)
            yield cls(
                settings,
                pool=pool,
                erasure=erasure,
                keys=keys,
                gate=RedisGate(redis, settings),
                domain=domain,
                gateway=gateway,
                pack=pack,
                graph=graph,
                retrieval=retrieval,
            )

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[Conn]:
        """A pooled app_rw connection, taken off the event loop. Whatever the caller did not commit
        is rolled back on the way out."""
        conn = await asyncio.to_thread(self.pool.getconn)
        try:
            yield conn
        finally:
            conn.rollback()
            self.pool.putconn(conn)

    # --- sessions -----------------------------------------------------------------------------
    async def create_session(
        self, channel: Literal["web", "app"], locale: Literal["en-IN", "hi-IN"]
    ) -> dict[str, str]:
        versions = await self.domain.get_versions()
        notice = await self.domain.get_current_consent_notice(locale)
        session_id, subject_ref = uuid7(), uuid7()
        key_ref = self.keys.create_subject_key(subject_ref)
        token = secrets.token_urlsafe(32)
        async with self.connection() as conn:
            pins = VersionPins(
                prompt_bundle=self.settings.prompt_bundle,
                rules=versions.rules_version,
                corpus=store.active_snapshots(conn),
                consent_notice=notice.notice_version,
                params=versions.params_version,
                ranker=versions.ranker_version,
                registry=versions.registry_version,
            )
            store.insert_session(
                conn,
                session_id=session_id,
                subject_ref=subject_ref,
                key_ref=key_ref,
                channel=channel,
                locale=locale,
                pins=pins.model_dump(mode="json"),
                expires_at=datetime.now(UTC) + timedelta(days=self.settings.session_ttl_days),
                token_sha256=token_sha256(token),
            )
            conn.commit()
        logger.info("session created: %s %s, rules %s", channel, locale, pins.rules)
        return {
            "session_id": str(session_id),
            "session_token": token,
            "events_url": f"/v1/sessions/{session_id}/events",
        }

    async def authenticate(self, session_id: UUID, token: str | None) -> SessionRow:
        """An unknown session and a wrong token are the same 401: the caller learns nothing. So is
        an erased session whose rows are not deleted yet (the sweep finishes it)."""
        async with self.connection() as conn:
            row = store.get_session(conn, session_id)
        presented = token_sha256(token) if token else _NO_TOKEN
        if not hmac.compare_digest(row.token_sha256 if row else _NO_TOKEN, presented) or not row:
            raise ProblemError(401, "UNAUTHORIZED")
        if row.status == "erased":
            raise ProblemError(401, "UNAUTHORIZED")
        if row.expires_at <= datetime.now(UTC):
            raise ProblemError(410, "SESSION_EXPIRED")
        return row

    # --- turns --------------------------------------------------------------------------------
    async def run_turn(
        self, row: SessionRow, turn_key: UUID, text: str | None, action: dict[str, Any] | None
    ) -> bytes:
        lock = await self.gate.acquire(row.session_id)  # a RedisError here is a 503: fail closed
        if lock is None:
            raise ProblemError(409, "SESSION_BUSY")
        try:
            if await self.gate.over_limit(row.subject_ref):
                raise ProblemError(429, "RATE_LIMITED")
            async with self.connection() as conn:
                turn = Turn(
                    settings=self.settings,
                    conn=conn,
                    keys=self.keys,
                    gateway=self.gateway,
                    domain=self.domain,
                    gate=self.gate,
                    pack=self.pack,
                    session_id=row.session_id,
                    turn_key=turn_key,
                    text=text,
                    action=action,
                    retrieval=self.retrieval,
                )
                await self._invoke(turn)
            if turn.erasure is not None and not turn.replayed:
                await self._erase(row, minor=turn.erasure == "MINOR")
            return canonical_json(turn.response)
        finally:
            try:
                await self.gate.release(row.session_id, lock)
            except RedisError:
                logger.warning("session lock not released; it expires on its own")

    async def _invoke(self, turn: Turn) -> None:
        try:
            await self.graph.ainvoke(
                {"turn_key": str(turn.turn_key)},
                {"configurable": {"thread_id": str(turn.session_id)}},
                context=turn,
                durability="exit",  # one checkpoint write per turn, after the commit
            )
        except psycopg.errors.LockNotAvailable:
            raise ProblemError(409, "SESSION_BUSY") from None
        except Exception as exc:
            if turn.response is not None:
                # Committed and released (I8 holds); only the checkpoint is missing, and the next
                # load hydrates from conv.
                logger.exception("turn committed but the checkpoint was not written")
                return
            if isinstance(exc, ProblemError):
                raise
            if isinstance(exc, (*UNAVAILABLE, LookupError)):
                logger.exception("turn failed before commit; nothing released")
                raise ProblemError(503, "SERVICE_UNAVAILABLE") from None
            raise
        if turn.response is None:
            raise RuntimeError("the graph ended without a released response")

    async def _erase(self, row: SessionRow, *, minor: bool) -> None:
        """The committed erasure turn's hard delete (graph/handlers/data_erasure.py). The reply is
        already the record of what was released, so a failure here is logged and left to the sweep;
        the session is marked erased and refused meanwhile."""
        try:
            await asyncio.to_thread(
                data_erasure.erase,
                self.erasure,
                self.keys,
                self.settings,
                session_id=row.session_id,
                key_ref=row.key_ref,
                minor=minor,
            )
        except Exception:
            logger.exception("erasure after the commit failed; the sweep retries it")
        await data_erasure.sweep(self.pool, self.erasure, self.keys, self.domain, self.settings)

    # --- internal -----------------------------------------------------------------------------
    async def verify(self, session_id: UUID) -> VerifyResult:
        async with self.connection() as conn:
            return audit_chain.verify_session(conn, session_id)

    async def audit_headers(self, session_id: UUID) -> list[dict[str, Any]]:
        """The session's events without their payloads: headers are non-personal by design."""
        async with self.connection() as conn:
            events = audit_chain.events(conn, session_id)
        return [
            {
                "seq": e.seq,
                "event_type": e.event_type,
                "occurred_at": e.occurred_at.isoformat(),
                "fsm_state": e.fsm_state,
                "header": e.header,
            }
            for e in events
        ]

    async def handoffs(self, queue: str) -> list[HandoffRow]:
        async with self.connection() as conn:
            return store.list_handoffs(conn, queue)

    async def handoff(self, handoff_id: UUID) -> tuple[HandoffRow, dict[str, Any]]:
        """The decrypted advisor briefing: 404 when unknown (or erased), 410 once the subject's
        key is destroyed."""
        async with self.connection() as conn:
            try:
                found = store.get_handoff(conn, self.keys, handoff_id)
            except KeyDestroyed:
                raise ProblemError(410, "GONE") from None
        if found is None:
            raise ProblemError(404, "NOT_FOUND")
        return found

    async def kill_switch(
        self,
        kind: Literal["product", "prompt_bundle", "route"],
        target: str,
        active: bool,
        reason_code: str,
        actor: str,
    ) -> UUID:
        """The domain tier withdraws a product first (with the internal-scope token), then the
        conv row and KILL_SWITCH commit together on the system chain. A retry is safe: the domain
        kill switch is idempotent."""
        if kind == "product":
            if not active:
                raise ProblemError(422, "KILL_SWITCH_IRREVERSIBLE")
            try:
                await self.domain.set_product_kill_switch(
                    target,
                    KillSwitch(reason=reason_code, actor=actor),
                    internal_token=self.settings.domain_internal_token.get_secret_value(),
                )
            except DomainError as exc:
                if exc.status == 404:
                    raise ProblemError(404, "NOT_FOUND") from None
                logger.warning("domain kill switch failed: %s", exc.code)
                raise ProblemError(503, "SERVICE_UNAVAILABLE") from None
        async with self.connection() as conn:
            switch_id = store.insert_kill_switch(
                conn, kind=kind, target=target, active=active, reason=reason_code, actor=actor
            )
            audit_chain.append(
                conn,
                self.keys,
                session_id=SYSTEM_SESSION,
                event_type=EventType.KILL_SWITCH,
                fsm_state="SYSTEM",
                pins={},
                header=KillSwitchHeader(
                    target_kind=kind,
                    target=target,
                    active=active,
                    reason_code=reason_code,
                    approvals_count=1,
                ),
                payload={"actor": actor},
                key_ref=SYSTEM_KEY_REF,
            )
            conn.commit()
        logger.info("kill switch %s %s active=%s", kind, target, active)
        return switch_id
