"""The model path against the running stack: chat routes through the gateway (the stubs until
Step 9), embed and rerank on TEI. Needs `make up`; no database settings."""

import json
import math
import os
from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio

from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.gateway import DataClass, Gateway, RedactionAttestation, Route
from surakshasetu.gateway.adapter import ROUTES

pytestmark = [pytest.mark.stack, pytest.mark.asyncio]

# Scripting talks to the stubs themselves, wherever the gateway points.
STUBS_URL = os.environ.get("SS_TEST_STUBS_URL", "http://127.0.0.1:8090")
SETTINGS = Settings(_env_file=None)
# Local TEI runs on CPU, and the TDD's embed and rerank budgets (150 and 300 ms) are GPU budgets.
# Measured on the 8 GB Docker VM (Step 8): a one-query embed is p50 50 ms but p95 229 ms, and a
# three-document rerank takes about 370 ms. These tests prove the round trip and the answers;
# tests/unit/test_gateway.py proves the budgets are enforced. Step 12 decides how a local run gets
# budgets of its own.
LOCAL_CPU_TIMEOUT_S = 30.0


@pytest.fixture(autouse=True)
def cpu_budgets(monkeypatch: pytest.MonkeyPatch) -> None:
    for route in (Route.EMBED, Route.RERANK):
        monkeypatch.setitem(ROUTES, route, ROUTES[route]._replace(timeout_s=LOCAL_CPU_TIMEOUT_S))


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[Gateway]:
    async with Gateway(SETTINGS) as gateway:
        yield gateway


async def chat(
    gateway: Gateway, route: Route, text: str, session_id: UUID | None = None
) -> tuple[str, str]:
    messages = [{"role": "user", "content": text}]
    redacted = route in (Route.GEN_CONVERSE, Route.GEN_RECOMMEND)
    result = await gateway.call(
        route,
        data_class=DataClass.REDACTED if redacted else DataClass.SELF_HOSTED_RAW,
        attestation=RedactionAttestation(sha256_hex(messages)) if redacted else None,
        messages=messages,
        session_id=session_id or uuid4(),
        turn_id=uuid4(),
        fsm_state="S2",
    )
    return result.served_model, result.content


@pytest.mark.parametrize(
    ("route", "served"),
    [
        (Route.GUARD_INPUT, "stub-guard"),
        (Route.NLU_EXTRACT, "stub-nlu"),
        (Route.GEN_CONVERSE, "stub-gen"),
        (Route.GEN_RECOMMEND, "stub-gen"),
        (Route.VERIFY_CLAIMS, "stub-verify"),
        (Route.SUMMARISE, "stub-nlu"),
    ],
)
async def test_every_chat_route_round_trips(gateway: Gateway, route: Route, served: str) -> None:
    served_model, content = await chat(gateway, route, "I am 34 and want term cover [R1] [E1]")

    assert served_model == served
    assert content


async def test_the_guard_route_sees_self_harm(gateway: Gateway) -> None:
    _, content = await chat(gateway, Route.GUARD_INPUT, "I want to end my life")

    assert json.loads(content)["safety"] == "unsafe S11"


async def test_a_script_reaches_only_its_session(gateway: Gateway) -> None:
    session_id = uuid4()
    async with httpx.AsyncClient(base_url=STUBS_URL) as stubs:
        queued = await stubs.post(
            "/__script",
            json={
                "session_id": str(session_id),
                "route": "gen-converse",
                "responses": ["scripted one", "scripted two"],
            },
        )
        queued.raise_for_status()
        try:
            first = await chat(gateway, Route.GEN_CONVERSE, "hi", session_id)
            other = await chat(gateway, Route.GEN_CONVERSE, "hi")
            second = await chat(gateway, Route.GEN_CONVERSE, "hi", session_id)
            after = await chat(gateway, Route.GEN_CONVERSE, "hi", session_id)
        finally:
            await stubs.delete(f"/__script/{session_id}")

    assert [first[1], second[1]] == ["scripted one", "scripted two"]
    assert other[1] == after[1] != "scripted one"


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True)) / math.sqrt(
        sum(x * x for x in a) * sum(y * y for y in b)
    )


async def test_embed_returns_the_configured_dimension_in_every_language(
    gateway: Gateway,
) -> None:
    texts = [
        "What does the term plan exclude?",
        "टर्म प्लान में क्या शामिल नहीं है?",
        "term plan mein kya cover nahi hota?",
        "The office cafeteria opens at nine.",
    ]

    queries = await gateway.embed(texts, kind="query")
    documents = await gateway.embed(texts[:1], kind="document")

    assert [len(v) for v in queries + documents] == [SETTINGS.embed_dim] * 5
    # Cross-lingual sanity: the Hindi and Hinglish questions sit closer to the English one than
    # an unrelated sentence does.
    english, hindi, hinglish, unrelated = queries
    assert cosine(english, hindi) > cosine(english, unrelated)
    assert cosine(english, hinglish) > cosine(english, unrelated)


@pytest.mark.parametrize(
    "query",
    ["Is suicide in the first year covered?", "pehle saal mein suicide cover hota hai kya?"],
)
async def test_rerank_puts_the_relevant_clause_first(gateway: Gateway, query: str) -> None:
    docs = [
        "The office cafeteria opens at nine and serves tea.",
        "Exclusions: suicide within twelve months of the risk commencement date is not covered; "
        "the nominee receives eighty per cent of the premiums paid.",
        "Premiums can be paid monthly, quarterly or annually.",
    ]

    scores = await gateway.rerank(query, docs)

    assert len(scores) == 3
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert max(range(3), key=scores.__getitem__) == 1
