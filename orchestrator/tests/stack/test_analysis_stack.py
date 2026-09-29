"""analysis.pipeline against the running stack: guard-input and nlu-extract through the real
OmniRoute -> stubs path, with a real audit chain. Needs `make up`, `make gateway-up` and
`make check-db`'s database env (SS_TEST_PG_DSN_ADMIN, SS_TEST_PG_DSN_KEYVAULT)."""

import json
import os
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

import httpx
import psycopg
import pytest
import pytest_asyncio

from surakshasetu.analysis.nlu import PendingSlotSpec
from surakshasetu.analysis.pipeline import TurnContext, analyse_turn
from surakshasetu.audit.chain import events as audit_events
from surakshasetu.config import Settings
from surakshasetu.crypto.keys import LocalKeyService
from surakshasetu.gateway import Gateway
from surakshasetu.uuid7 import uuid7

pytestmark = [pytest.mark.stack, pytest.mark.asyncio]

STUBS_URL = os.environ.get("SS_TEST_STUBS_URL", "http://127.0.0.1:8090")
SETTINGS = Settings(_env_file=None)

Conn = psycopg.Connection[tuple[Any, ...]]


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Gateway]:
    async with Gateway(SETTINGS) as gateway:
        yield gateway


def _ctx(*, session_id: UUID, key_ref: str) -> TurnContext:
    return TurnContext(
        session_id=session_id,
        turn_id=uuid7(),
        turn_seq=1,
        turn_key=uuid7(),
        fsm_state="S1",
        pins={"rules": "r-1"},
        channel="web",
        key_ref=key_ref,
        pending=PendingSlotSpec(pending_slot="age"),
    )


async def test_an_unscripted_turn_round_trips_and_is_fully_audited(
    db: Conn, keys: LocalKeyService, gateway: Gateway
) -> None:
    session_id, subject_ref = uuid7(), uuid7()
    key_ref = keys.create_subject_key(subject_ref)

    result = await analyse_turn(
        db,
        keys,
        gateway,
        "I am 34 years old",
        _ctx(session_id=session_id, key_ref=key_ref),
        settings=SETTINGS,
    )

    assert result.blocked is False
    assert result.analysis is not None
    assert len(audit_events(db, session_id)) == 5


async def test_a_scripted_injection_attempt_is_blocked(
    db: Conn, keys: LocalKeyService, gateway: Gateway
) -> None:
    session_id, subject_ref = uuid7(), uuid7()
    key_ref = keys.create_subject_key(subject_ref)
    async with httpx.AsyncClient(base_url=STUBS_URL) as stubs:
        await stubs.post(
            "/__script",
            json={
                "session_id": str(session_id),
                "route": "guard-input",
                "responses": [json.dumps({"injection_score": 0.97, "safety": "safe"})],
            },
        )
        try:
            result = await analyse_turn(
                db,
                keys,
                gateway,
                "What is my premium?",
                _ctx(session_id=session_id, key_ref=key_ref),
                settings=SETTINGS,
            )
        finally:
            await stubs.delete(f"/__script/{session_id}")

    assert result.blocked is True
    assert result.block_reason == "injection"


async def test_a_self_harm_message_is_blocked_as_safety(
    db: Conn, keys: LocalKeyService, gateway: Gateway
) -> None:
    session_id, subject_ref = uuid7(), uuid7()
    key_ref = keys.create_subject_key(subject_ref)

    result = await analyse_turn(
        db,
        keys,
        gateway,
        "I want to end my life",
        _ctx(session_id=session_id, key_ref=key_ref),
        settings=SETTINGS,
    )

    assert result.blocked is True
    assert result.block_reason == "safety"


async def test_a_scripted_slot_with_a_bad_evidence_span_is_dropped(
    db: Conn, keys: LocalKeyService, gateway: Gateway
) -> None:
    session_id, subject_ref = uuid7(), uuid7()
    key_ref = keys.create_subject_key(subject_ref)
    scripted = json.dumps(
        {
            "intents": ["SLOT_ANSWER"],
            "slots": [
                {"slot": "age", "value": 34, "confidence": 0.9, "evidence_span": "34"},
                {
                    "slot": "annual_income_inr",
                    "value": 500000,
                    "confidence": 0.7,
                    "evidence_span": "5 lakh",  # not a substring of the turn text
                },
            ],
            "side_query": None,
            "language": "en",
        }
    )
    async with httpx.AsyncClient(base_url=STUBS_URL) as stubs:
        await stubs.post(
            "/__script",
            json={"session_id": str(session_id), "route": "nlu-extract", "responses": [scripted]},
        )
        try:
            result = await analyse_turn(
                db,
                keys,
                gateway,
                "I am 34",
                _ctx(session_id=session_id, key_ref=key_ref),
                settings=SETTINGS,
            )
        finally:
            await stubs.delete(f"/__script/{session_id}")

    assert result.analysis is not None
    assert [s.slot for s in result.analysis.slots] == ["age"]
