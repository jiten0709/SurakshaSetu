"""Degraded retrieval (TDD §1.5: embed or rerank down -> BM25-only with a stricter threshold) and
the pins retrieval refuses rather than guess on."""

import logging
from pathlib import Path

import pytest
from retrieval_support import FakeTei, ctx, metas, payload, service

from surakshasetu.gateway import GatewayUnavailable, TeiModel
from surakshasetu.retrieval.gate import NO_SUFFICIENT_EVIDENCE
from surakshasetu.retrieval.service import RetrievalUnavailable

LEXICAL = payload("regulatory", "mc", ["IRDAI", "MC", "5. Free look"], "DUMMY: free look period.")
SEMANTIC = payload("regulatory", "gr", ["Insurer", "Grievance", "3. Timelines"], "DUMMY: fourteen.")
BOTH = [LEXICAL, SEMANTIC]
QUESTION = "What is the free look period?"


@pytest.mark.asyncio
async def test_hybrid_search_finds_what_bm25_alone_cannot(tmp_path: Path) -> None:
    svc = await service(tmp_path, BOTH, FakeTei({LEXICAL.text: 0.9, SEMANTIC.text: 0.1}))

    result = await svc.retrieve(QUESTION, ctx())

    assert set(result.audit.chunk_ids) == {LEXICAL.chunk_id, SEMANTIC.chunk_id}
    assert not result.audit.degraded


@pytest.mark.asyncio
async def test_embed_down_searches_bm25_only_and_still_reranks(tmp_path: Path) -> None:
    tei = FakeTei({LEXICAL.text: 0.9}, embed_down=True)
    svc = await service(tmp_path, BOTH, tei)

    result = await svc.retrieve(QUESTION, ctx())

    assert result.audit.chunk_ids == [LEXICAL.chunk_id]  # no dense: SEMANTIC shares no word
    assert (result.audit.degraded, result.audit.scoring) == (True, "rerank")
    assert result.evidence[0].rerank_score == 0.9
    assert len(tei.reranked) == 1


@pytest.mark.asyncio
async def test_degraded_retrieval_needs_the_stricter_threshold(tmp_path: Path) -> None:
    # 0.55 passes the 0.5 threshold, but not 0.5 x 1.25 once embed is down.
    up = await service(tmp_path, BOTH, FakeTei({LEXICAL.text: 0.55}))
    down = await service(tmp_path, BOTH, FakeTei({LEXICAL.text: 0.55}, embed_down=True))

    assert not (await up.retrieve(QUESTION, ctx())).abstained
    degraded = await down.retrieve(QUESTION, ctx())
    assert (degraded.abstained, degraded.abstain_reason) == (True, NO_SUFFICIENT_EVIDENCE)


@pytest.mark.asyncio
async def test_a_degraded_threshold_above_one_always_abstains(tmp_path: Path) -> None:
    svc = await service(tmp_path, BOTH, FakeTei({LEXICAL.text: 1.0}, embed_down=True), rerank=0.9)

    assert (await svc.retrieve(QUESTION, ctx())).abstained


@pytest.mark.asyncio
async def test_rerank_down_searches_bm25_only_and_scores_by_bm25(tmp_path: Path) -> None:
    svc = await service(tmp_path, BOTH, FakeTei(rerank_down=True), bm25_threshold=0.1)

    result = await svc.retrieve(QUESTION, ctx())

    assert result.audit.chunk_ids == [LEXICAL.chunk_id]  # re-searched without the dense half
    assert (result.audit.degraded, result.audit.scoring) == (True, "bm25")
    bm25_score = result.audit.rerank_scores[0]
    assert bm25_score is not None and bm25_score > 0.1 * 1.25
    assert not result.abstained
    assert result.evidence[0].rerank_score is None  # it was never reranked


@pytest.mark.asyncio
async def test_rerank_down_gates_on_the_bm25_threshold(tmp_path: Path) -> None:
    svc = await service(tmp_path, BOTH, FakeTei(rerank_down=True), bm25_threshold=1000.0)

    assert (await svc.retrieve(QUESTION, ctx())).abstain_reason == NO_SUFFICIENT_EVIDENCE


@pytest.mark.asyncio
async def test_a_failed_rerank_call_falls_back_like_a_missing_reranker(tmp_path: Path) -> None:
    # The reranker answers /info, then times out on the rerank itself.
    tei = FakeTei({LEXICAL.text: 0.9})
    svc = await service(tmp_path, BOTH, tei, bm25_threshold=0.1)

    async def timeout(query: str, docs: list[str]) -> list[float]:
        raise GatewayUnavailable("TIMEOUT")

    tei.rerank = timeout  # type: ignore[method-assign]

    result = await svc.retrieve(QUESTION, ctx())

    assert (result.audit.scoring, result.audit.degraded) == ("bm25", True)


@pytest.mark.asyncio
async def test_a_snapshot_embedded_by_another_model_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    other = TeiModel("org/embedder", "a-newer-revision")
    svc = await service(tmp_path, BOTH, FakeTei({LEXICAL.text: 0.9}, embed_model=other))

    with caplog.at_level(logging.ERROR), pytest.raises(RetrievalUnavailable) as failed:
        await svc.retrieve(QUESTION, ctx())

    assert failed.value.reason == "EMBED_MODEL_MISMATCH"
    assert "corpus defect" in caplog.text


@pytest.mark.parametrize(
    ("recorded", "reason"),
    [
        ({"regulatory": {"chunk_count": 3}}, "SNAPSHOT_MISSING"),  # points gone from Qdrant
        ({"regulatory": {"analyzer_version": "bm25-2025.01.1"}}, "ANALYZER_MISMATCH"),
        ({"regulatory": {"collection": "tax"}}, "SNAPSHOT_UNKNOWN"),
        # A missing collection is Qdrant's 404, which only the real server gives: stack-tested.
    ],
)
@pytest.mark.asyncio
async def test_a_pin_that_does_not_hold_is_a_corpus_defect(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    recorded: dict,
    reason: str,  # type: ignore[type-arg]
) -> None:
    svc = await service(
        tmp_path, BOTH, FakeTei({LEXICAL.text: 0.9}), recorded=metas(BOTH, **recorded)
    )

    with caplog.at_level(logging.ERROR), pytest.raises(RetrievalUnavailable) as failed:
        await svc.retrieve(QUESTION, ctx())

    assert failed.value.reason == reason
    assert "corpus defect" in caplog.text


@pytest.mark.asyncio
async def test_an_unrecorded_pin_is_refused(tmp_path: Path) -> None:
    svc = await service(tmp_path, BOTH, FakeTei())

    pins = {"regulatory": "regulatory-1999-01-01", "product": "product-test", "tax": "tax-test"}

    with pytest.raises(RetrievalUnavailable) as failed:
        await svc.retrieve(QUESTION, ctx(corpus_pins=pins))

    assert failed.value.reason == "SNAPSHOT_UNKNOWN"


@pytest.mark.asyncio
async def test_a_routed_collection_without_a_pin_is_refused(tmp_path: Path) -> None:
    svc = await service(tmp_path, BOTH, FakeTei())

    with pytest.raises(RetrievalUnavailable) as failed:
        await svc.retrieve(QUESTION, ctx(corpus_pins={"regulatory": "regulatory-test"}))

    assert failed.value.reason == "PIN_MISSING"


@pytest.mark.asyncio
async def test_a_verified_pin_is_read_once(tmp_path: Path) -> None:
    recorded = metas(BOTH)
    reads: list[str] = []

    def read(snapshot_id: str):  # type: ignore[no-untyped-def]
        reads.append(snapshot_id)
        return recorded.get(snapshot_id)

    svc = await service(tmp_path, BOTH, FakeTei({LEXICAL.text: 0.9}))
    svc._snapshot_meta = read

    await svc.retrieve(QUESTION, ctx())
    await svc.retrieve(QUESTION, ctx())

    assert sorted(reads) == ["product-test", "regulatory-test", "tax-test"]
