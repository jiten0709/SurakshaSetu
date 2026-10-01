"""The retrieval service against the real Qdrant, TEI and the dev catalog's corpus snapshots (Step
11's seed corpora). Needs `make up && make kb-ingest && make calibrate-retrieval`, and
SS_TEST_PG_DSN_KB (app_rw on surakshasetu, read only), which `make check-stack` sets.

CPU TEI misses the GPU budgets, so the TEI timeouts are scaled for this test, as dev allows. Each
case narrows its route or its focus so a CPU rerank stays within seconds.
"""

import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import Any

import pytest
import pytest_asyncio
from psycopg_pool import ConnectionPool
from qdrant_client import AsyncQdrantClient

from surakshasetu.config import Settings
from surakshasetu.gateway import Gateway
from surakshasetu.kb.golden import load_golden
from surakshasetu.retrieval.rewrite import KB_CONFIG
from surakshasetu.retrieval.service import (
    RetrievalContext,
    RetrievalService,
    RetrievalUnavailable,
    SnapshotMeta,
    load_snapshot_meta,
)

pytestmark = [pytest.mark.stack, pytest.mark.asyncio]

GOLDEN = KB_CONFIG.parent / "golden" / "retrieval"
PINS = {c: load_golden(GOLDEN / f"{c}.yaml").snapshot_id for c in ("regulatory", "product", "tax")}


@pytest.fixture(scope="module")
def pool() -> Iterator[ConnectionPool[Any]]:
    dsn = os.environ.get("SS_TEST_PG_DSN_KB")
    if not dsn:
        pytest.fail("SS_TEST_PG_DSN_KB is not set; run `make check-stack`")
    with ConnectionPool(dsn, min_size=1) as opened:
        yield opened


def settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, tei_timeout_scale=1000, **overrides)


@pytest_asyncio.fixture
async def service(pool: ConnectionPool[Any]) -> AsyncIterator[RetrievalService]:
    async with Gateway(settings()) as gateway:
        yield RetrievalService(
            gateway, AsyncQdrantClient(url=settings().qdrant_url), partial(load_snapshot_meta, pool)
        )


def ctx(**overrides: Any) -> RetrievalContext:
    fields: dict[str, Any] = {
        "fsm_state": "S3",
        "language": "en",
        "as_of": datetime.now(UTC),
        "corpus_pins": PINS,
    }
    return RetrievalContext(**(fields | overrides))


async def test_an_s0_privacy_question_is_answered_from_regulatory_text(
    service: RetrievalService,
) -> None:
    result = await service.retrieve(
        "Is a pre-ticked checkbox valid consent under the data protection law?",
        ctx(fsm_state="S0"),
    )

    assert not result.abstained, result.abstain_reason
    assert result.audit.collections == ["regulatory"]
    assert result.evidence[0].handle == "E1"
    assert result.evidence[0].chunk_id == "regulatory:dpdp-act-2023-rules-2025:6:1245ea"
    assert {e.domain for e in result.evidence} == {"regulatory"}


async def test_a_question_only_the_pending_draft_answers_abstains(
    service: RetrievalService,
) -> None:
    # una-02: the exposure draft is pending review, so it is not searchable at all.
    result = await service.retrieve(
        "What cooling-off period does the IRDAI exposure draft on conversational AI propose?",
        ctx(fsm_state="S0", entities=["privacy"]),
    )

    assert result.abstained
    assert not any("irdai-ed-ai-2026" in c for c in result.audit.chunk_ids)


async def test_a_focus_uin_keeps_other_products_out(service: RetrievalService) -> None:
    result = await service.retrieve(
        "Does this plan pay anything if I survive to the end of the policy term?",
        ctx(fsm_state="S2", focus_uins=["999N001V02"], intents=["SPECIFIC_PLAN"]),
    )

    assert result.audit.collections == ["product"]
    assert result.audit.candidates["product"] == 11  # the three documents of 999N001V02
    assert all(":999N001V02:" in c for c in result.audit.chunk_ids)
    assert result.audit.rewritten_query.startswith("Does 999N001V02 pay")


async def test_embed_down_degrades_to_bm25_only(pool: ConnectionPool[Any]) -> None:
    async with Gateway(settings(tei_embed_url="http://127.0.0.1:9")) as gateway:
        degraded = RetrievalService(
            gateway, AsyncQdrantClient(url=settings().qdrant_url), partial(load_snapshot_meta, pool)
        )
        selection = await degraded.select(
            "Is a pre-ticked checkbox valid consent?", ctx(fsm_state="S0")
        )

    assert (selection.degraded, selection.scoring) == (True, "rerank")
    assert selection.evidence, "BM25 alone still finds the consent clause"


async def test_an_unrecorded_pin_is_unavailable(service: RetrievalService) -> None:
    with pytest.raises(RetrievalUnavailable) as failed:
        await service.retrieve(
            "Can I withdraw my consent?",
            ctx(fsm_state="S0", corpus_pins=PINS | {"regulatory": "regulatory-1999-01-01"}),
        )

    assert failed.value.reason == "SNAPSHOT_UNKNOWN"


async def test_a_snapshot_whose_collection_is_gone_is_missing(pool: ConnectionPool[Any]) -> None:
    def elsewhere(snapshot_id: str) -> SnapshotMeta | None:
        meta = load_snapshot_meta(pool, snapshot_id)
        return None if meta is None else replace(meta, qdrant_collection="kb_does_not_exist")

    async with Gateway(settings()) as gateway:
        service = RetrievalService(gateway, AsyncQdrantClient(url=settings().qdrant_url), elsewhere)
        with pytest.raises(RetrievalUnavailable) as failed:
            await service.retrieve("Can I withdraw my consent?", ctx(fsm_state="S0"))

    assert failed.value.reason == "SNAPSHOT_MISSING"


async def test_a_snapshot_stamped_with_another_embedder_is_unavailable(
    pool: ConnectionPool[Any],
) -> None:
    def restamped(snapshot_id: str) -> SnapshotMeta | None:
        meta = load_snapshot_meta(pool, snapshot_id)
        if meta is None:
            return None
        return replace(meta, embed_model=replace(meta.embed_model, model_sha="0" * 40))

    async with Gateway(settings()) as gateway:
        service = RetrievalService(gateway, AsyncQdrantClient(url=settings().qdrant_url), restamped)
        with pytest.raises(RetrievalUnavailable) as failed:
            await service.retrieve("Can I withdraw my consent?", ctx(fsm_state="S0"))

    assert failed.value.reason == "EMBED_MODEL_MISMATCH"
