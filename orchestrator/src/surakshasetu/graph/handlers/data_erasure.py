"""Data Erasure (rows CC1, CC1b and S1.0; I5; TDD §4.4).

In the turn (`node`, after decide):
- the Consent Service withdraws the consent record;
- the turn audits ERASURE_REQUEST, plus CONSENT_WITHDRAWN once the service has recorded it;
- the reply is the erasure template.
If the Consent Service fails, erasure still goes ahead. The header says "pending", the reply never
says "withdrawn", and `sweep` retries the withdrawal later.

After the run (`erase`, from Runtime.run_turn):
- the live conv rows and the checkpoint are hard-deleted as erasure_rw, children first (no FK
  cascades);
- the subject key is scheduled for destruction at the end of the longest retention, or destroyed at
  once for a minor.
This waits until after the graph for two reasons. LangGraph writes the checkpoint when the run exits
(durability="exit"), and the turn's own transaction holds the session row until its commit; a
minor's key must also outlive that commit, which encrypts the release.

Audit rows stay: they are the legal record, and crypto-shredding covers them. commit marks the
session "erased" first, so a failed delete leaves an inert session (authentication refuses it) that
`sweep` finishes.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import UUID

import psycopg
from langgraph.runtime import Runtime
from psycopg_pool import ConnectionPool

from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import ConsentWithdrawnHeader, ErasureRequestHeader, EventType
from surakshasetu.config import Settings
from surakshasetu.crypto.keys import KeyDestroyed, KeyService
from surakshasetu.domain.client import DomainClient, DomainError
from surakshasetu.domain.models import ConsentRecord, ConsentWithdrawal
from surakshasetu.fsm.transition import Transition
from surakshasetu.graph.handlers import append, granted, scripts, session
from surakshasetu.graph.state import GraphState
from surakshasetu.store.conv import Conn

logger = logging.getLogger(__name__)

ERASE = "ERASE"  # the structured action, and DELETE /v1/sessions/{id}
Reason = Literal["WITHDRAW", "MINOR"]

# Children first: disclosure_ack references recommendation, and the rest reference the session
# (slot_value also references turn). Then the checkpointer's rows for the thread.
_DELETES = (
    "DELETE FROM conv.disclosure_ack WHERE rec_id IN"
    " (SELECT rec_id FROM conv.recommendation WHERE session_id = %(session)s)",
    "DELETE FROM conv.recommendation WHERE session_id = %(session)s",
    "DELETE FROM conv.handoff WHERE session_id = %(session)s",
    "DELETE FROM conv.slot_value WHERE session_id = %(session)s",
    "DELETE FROM conv.turn WHERE session_id = %(session)s",
    "DELETE FROM conv.session WHERE session_id = %(session)s",
    "DELETE FROM langgraph.checkpoint_writes WHERE thread_id = %(thread)s",
    "DELETE FROM langgraph.checkpoint_blobs WHERE thread_id = %(thread)s",
    "DELETE FROM langgraph.checkpoints WHERE thread_id = %(thread)s",
)
# ponytail: a scan of the audit table without an index; a partial index or Step 24's job takes
# over if erasures become frequent.
_PENDING = (
    "SELECT e.session_id, e.key_ref, e.pins, e.header->>'consent_id' FROM audit.audit_event e"
    " WHERE e.event_type = 'ERASURE_REQUEST' AND e.header->>'consent_withdrawal' = 'pending'"
    " AND NOT EXISTS (SELECT 1 FROM audit.audit_event w WHERE w.session_id = e.session_id"
    " AND w.event_type = 'CONSENT_WITHDRAWN' AND w.seq > e.seq)"
)


async def withdraw_consent(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """The turn router's target for a withdrawal (I5: its first check). It skips the state node, so
    nothing else in the turn is processed. decide then enters DATA_ERASURE (CC1), or stays in a
    closed state, and `node` erases either way."""


def reason_of(transition: Transition) -> Reason:
    return "MINOR" if transition.reason_code == "MINOR" else "WITHDRAW"


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    current = session(turn)
    reason = reason_of(cast(Transition, turn.transition))
    consent = current.consent
    withdrawal: Literal["done", "pending", "none"] = "none"
    record: ConsentRecord | None = None
    if consent is not None and consent.withdrawn_at is not None:
        withdrawal = "done"  # withdrawn before; nothing more to record
    elif consent is not None:
        try:
            record = await turn.domain.withdraw_consent(
                consent.consent_id, ConsentWithdrawal(reason=reason)
            )
            current.consent, withdrawal = record, "done"
        except DomainError as exc:
            logger.warning("consent withdrawal not recorded (%s): erasing; retried later", exc.code)
            withdrawal = "pending"
    append(
        turn,
        EventType.ERASURE_REQUEST,
        ErasureRequestHeader(
            reason_code=reason,
            consent_id=consent.consent_id if consent else None,
            consent_withdrawal=withdrawal,
        ),
        {},
    )
    if record is not None:
        append(
            turn,
            EventType.CONSENT_WITHDRAWN,
            ConsentWithdrawnHeader(
                consent_id=record.consent_id,
                purposes=cast(Any, granted(record.purposes)),
            ),
            {"withdrawn_at": _iso(record.withdrawn_at)},
        )
    turn.erasure = reason
    texts = scripts(turn)
    if reason == "MINOR":
        turn.parts = [("minor_exit", texts.minor_exit)]
    elif withdrawal == "pending":
        turn.parts = [("erasure_pending", texts.erasure_pending)]
    else:
        turn.parts = [("erasure_done", texts.erasure_done)]
    logger.info("erasure requested (%s): consent withdrawal %s", reason, withdrawal)


def erase_session(pool: ConnectionPool[Conn], session_id: UUID) -> tuple[int, int]:
    """Hard-delete one session's live rows in one erasure_rw transaction. Returns (conv rows,
    checkpoint rows). Step 24's TTL purge reuses it."""
    params = {"session": session_id, "thread": str(session_id)}
    with pool.connection() as conn:  # commits on exit
        counts = [conn.execute(sql, params).rowcount for sql in _DELETES]
    return sum(counts[:6]), sum(counts[6:])


def erase(
    pool: ConnectionPool[Conn],
    keys: KeyService,
    settings: Settings,
    *,
    session_id: UUID,
    key_ref: str,
    minor: bool,
) -> None:
    conv_rows, checkpoint_rows = erase_session(pool, session_id)
    if minor:
        keys.destroy(key_ref)  # V2: nothing retained
    else:
        after = datetime.now(UTC) + timedelta(days=settings.key_retention_days)
        keys.schedule_destruction(key_ref, after)
    logger.info(
        "session erased: %d conv rows, %d checkpoint rows; key %s",
        conv_rows,
        checkpoint_rows,
        "destroyed" if minor else "scheduled for destruction",
    )


def _last_reason(conn: Conn, session_id: UUID) -> str | None:
    row = conn.execute(
        "SELECT header->>'reason_code' FROM audit.audit_event"
        " WHERE session_id = %s AND event_type = 'ERASURE_REQUEST' ORDER BY seq DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return None if row is None else row[0]


async def sweep(
    app: ConnectionPool[Conn],
    erasure: ConnectionPool[Conn],
    keys: KeyService,
    domain: DomainClient,
    settings: Settings,
) -> None:
    """Finish what an erasure turn could not, at startup and after every erasure. It never raises.
    - Sessions still marked erased (the delete after the commit failed) are erased.
    - Withdrawals the Consent Service did not record (it was down) are retried. On success,
      CONSENT_WITHDRAWN joins the session's chain. If a minor's key is already destroyed, nothing
      can be appended, and the consent ledger is the record (ponytail: such a withdrawal is re-sent,
      idempotently, at every sweep)."""
    try:
        with erasure.connection() as conn:
            leftover = conn.execute(
                "SELECT session_id, key_ref FROM conv.session WHERE status = 'erased'"
            ).fetchall()
        for session_id, key_ref in leftover:
            with app.connection() as conn:
                minor = _last_reason(conn, session_id) == "MINOR"
            erase(erasure, keys, settings, session_id=session_id, key_ref=key_ref, minor=minor)
        with app.connection() as conn:
            pending = conn.execute(_PENDING).fetchall()
    except psycopg.Error:
        logger.exception("erasure sweep failed; it runs again after the next erasure")
        return
    for session_id, key_ref, pins, consent_id in pending:
        try:
            retry = ConsentWithdrawal(reason="RETRY")
            record = await domain.withdraw_consent(UUID(consent_id), retry)
        except DomainError as exc:
            logger.warning("consent withdrawal still pending (%s)", exc.code)
            continue
        try:
            with app.connection() as conn:  # commits on exit
                audit_chain.append(
                    conn,
                    keys,
                    session_id=session_id,
                    event_type=EventType.CONSENT_WITHDRAWN,
                    fsm_state="DATA_ERASURE",
                    pins=pins,
                    header=ConsentWithdrawnHeader(
                        consent_id=record.consent_id,
                        purposes=cast(Any, granted(record.purposes)),
                    ),
                    payload={"withdrawn_at": _iso(record.withdrawn_at)},
                    key_ref=key_ref,
                )
            logger.info("pending consent withdrawal recorded")
        except KeyDestroyed:
            logger.warning("consent withdrawn after the key was destroyed: the ledger records it")
        except psycopg.Error:
            logger.exception("pending consent withdrawal not appended; retried at the next sweep")


def _iso(at: datetime | None) -> str | None:
    return at.isoformat() if at else None
