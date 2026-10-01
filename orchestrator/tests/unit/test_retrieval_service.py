from pathlib import Path

import pytest
from retrieval_support import FakeTei, ctx, payload, service

from surakshasetu.logging import configure_logging

SENTINEL_QUERY = "What is the free look period for Zqxv Sentinelson?"
SENTINEL_TEXT = "DUMMY: free look period is thirty days, Qwzx-chunk-sentinel."
INTRO = payload(
    "regulatory", "mc", ["IRDAI", "MC"], "DUMMY: this circular sets the free look rules."
)
CLAUSE = payload("regulatory", "mc", ["IRDAI", "MC", "5. Free look"], SENTINEL_TEXT)
BROCHURE = payload(
    "product",
    "999N001V02:br-v1",
    ["Term (999N001V02)", "Brochure v1", "2. Free look"],
    "DUMMY: return the policy within the free look period.",
    doc_type="brochure",
)
CORPUS = [INTRO, CLAUSE, BROCHURE]
SCORES = {CLAUSE.text: 0.92, BROCHURE.text: 0.71, INTRO.text: 0.2}


@pytest.mark.asyncio
async def test_evidence_carries_turn_local_handles_and_citable_fields(tmp_path: Path) -> None:
    svc = await service(tmp_path, CORPUS, FakeTei(SCORES))

    result = await svc.retrieve(SENTINEL_QUERY, ctx())

    assert [(e.handle, e.chunk_id, e.parent) for e in result.evidence] == [
        ("E1", CLAUSE.chunk_id, False),
        ("E2", BROCHURE.chunk_id, False),
        ("E3", INTRO.chunk_id, False),
    ]
    first = result.evidence[0]
    assert (first.citation_label, first.text, first.domain) == (
        "mc §5",
        SENTINEL_TEXT,
        "regulatory",
    )
    assert (first.content_sha256, first.section_path) == (
        CLAUSE.content_sha256,
        CLAUSE.section_path,
    )
    assert first.rerank_score == 0.92
    assert result.audit.handle_map == {e.handle: e.chunk_id for e in result.evidence}


@pytest.mark.asyncio
async def test_each_chunk_carries_its_tdd_precedence(tmp_path: Path) -> None:
    svc = await service(tmp_path, CORPUS, FakeTei(SCORES))

    result = await svc.retrieve(SENTINEL_QUERY, ctx())

    ranks = {e.doc_type: e.precedence for e in result.evidence}
    assert ranks["master_circular"] < ranks["brochure"]
    assert [e.chunk_id for e in result.evidence][0] == CLAUSE.chunk_id  # still in rerank order


@pytest.mark.asyncio
async def test_the_audit_holds_ids_and_hashes_never_chunk_text(tmp_path: Path) -> None:
    svc = await service(tmp_path, CORPUS, FakeTei(SCORES))

    audit = (await svc.retrieve(SENTINEL_QUERY, ctx())).audit
    dumped = audit.model_dump_json()

    assert "Qwzx-chunk-sentinel" not in dumped
    assert audit.content_sha256[0] == CLAUSE.content_sha256
    assert audit.rerank_scores[:2] == [0.92, 0.71]
    assert (audit.collections, audit.snapshot_ids) == (
        ["regulatory", "product", "tax"],
        ["regulatory-test", "product-test", "tax-test"],
    )
    assert audit.candidates == {"regulatory": 2, "product": 1, "tax": 0}
    assert (audit.route_rule, audit.rewritten_query) == ("RT-DEFAULT", SENTINEL_QUERY)


@pytest.mark.asyncio
async def test_retrieval_is_deterministic(tmp_path: Path) -> None:
    svc = await service(tmp_path, CORPUS, FakeTei(SCORES))

    first = await svc.retrieve(SENTINEL_QUERY, ctx())
    second = await svc.retrieve(SENTINEL_QUERY, ctx())

    assert first == second


@pytest.mark.asyncio
async def test_the_rerank_reads_the_breadcrumb_and_the_lexical_query(tmp_path: Path) -> None:
    tei = FakeTei(SCORES)
    svc = await service(tmp_path, CORPUS, tei)

    await svc.retrieve("yeh plan ka 80C fayda?", ctx(focus_uins=["999N001V02"]))

    query, docs = tei.reranked[0]
    semantic = "999N001V02 ka 80C fayda? (section 123; Schedule XV)"
    assert query == f"{semantic} benefit"  # the lexicon's English terms help cross-lingual rerank
    assert tei.embedded == [semantic]  # dense search keeps the original
    assert all("\n\n" in d and " › " in d for d in docs)


@pytest.mark.asyncio
async def test_no_query_or_chunk_text_reaches_the_logs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restore_logging: None
) -> None:
    logs = tmp_path / "logs"
    configure_logging("DEBUG", logs)
    svc = await service(tmp_path, CORPUS, FakeTei(SCORES, embed_down=True))

    await svc.retrieve(SENTINEL_QUERY, ctx())
    weak = await service(tmp_path, CORPUS, FakeTei({}))
    await weak.retrieve(SENTINEL_QUERY, ctx())

    written = capsys.readouterr().out + "".join(
        p.read_text(encoding="utf-8") for p in logs.rglob("*.log")
    )
    assert "retrieval" in written and "abstained" in written and "degraded" in written
    for leaked in ("Sentinelson", "Qwzx", "free look"):
        assert leaked not in written
