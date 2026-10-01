from pathlib import Path

import pytest
from retrieval_support import RERANK_MODEL, FakeTei, ctx, payload, service

from surakshasetu.gateway import TeiModel
from surakshasetu.retrieval.gate import (
    NO_SUFFICIENT_EVIDENCE,
    QUOTA_UNMET,
    Scored,
    insufficient,
)
from surakshasetu.retrieval.routing import I3_PRODUCT_GATED, NOT_ROUTED
from surakshasetu.retrieval.service import RetrievalUnavailable

REG = payload("regulatory", "mc", ["IRDAI", "MC", "5. Free look"], "DUMMY: free look 30 days.")
PROD = payload(
    "product",
    "999N001V02:pw-v2",
    ["Term (999N001V02)", "PW v2", "3. Exclusions"],
    "DUMMY: suicide within twelve months is excluded.",
)


def threshold(p: object) -> float:
    return 0.5


def test_the_best_score_must_reach_its_threshold() -> None:
    assert insufficient([Scored(REG, 0.49)], {}, threshold) == NO_SUFFICIENT_EVIDENCE
    assert insufficient([Scored(REG, 0.5)], {}, threshold) is None
    assert insufficient([], {}, threshold) == NO_SUFFICIENT_EVIDENCE


def test_a_quota_needs_passing_chunks_not_just_chunks() -> None:
    kept = [Scored(REG, 0.9), Scored(PROD, 0.2)]

    assert insufficient(kept, {"product": 1}, threshold) == QUOTA_UNMET
    assert insufficient(kept, {"regulatory": 1}, threshold) is None


def test_parents_never_carry_the_gate() -> None:
    assert insufficient([Scored(REG, 0.9, parent=True)], {}, threshold) == NO_SUFFICIENT_EVIDENCE


def test_thresholds_are_per_snapshot() -> None:
    def per_domain(p: object) -> float:
        return {"regulatory": 0.8, "product": 0.3}[p.domain]  # type: ignore[attr-defined]

    assert insufficient([Scored(REG, 0.6)], {}, per_domain) == NO_SUFFICIENT_EVIDENCE
    assert insufficient([Scored(PROD, 0.6)], {}, per_domain) is None


@pytest.mark.asyncio
async def test_strong_evidence_is_released_with_handles(tmp_path: Path) -> None:
    svc = await service(tmp_path, [REG, PROD], FakeTei({REG.text: 0.9, PROD.text: 0.7}))

    result = await svc.retrieve("What is the free-look period?", ctx())

    assert not result.abstained
    assert [(e.handle, e.chunk_id) for e in result.evidence] == [
        ("E1", REG.chunk_id),
        ("E2", PROD.chunk_id),
    ]


@pytest.mark.asyncio
async def test_weak_evidence_abstains(tmp_path: Path) -> None:
    svc = await service(tmp_path, [REG, PROD], FakeTei({REG.text: 0.3, PROD.text: 0.2}))

    result = await svc.retrieve("What is the free-look period?", ctx())

    assert (result.abstained, result.abstain_reason, result.evidence) == (
        True,
        NO_SUFFICIENT_EVIDENCE,
        [],
    )
    assert result.audit.chunk_ids == [REG.chunk_id, PROD.chunk_id]  # kept for the audit
    assert result.audit.handle_map == {}  # but no handle was issued


@pytest.mark.asyncio
async def test_an_exclusion_question_needs_a_passing_product_chunk(tmp_path: Path) -> None:
    svc = await service(tmp_path, [REG, PROD], FakeTei({REG.text: 0.9, PROD.text: 0.2}))

    result = await svc.retrieve("What is the suicide exclusion?", ctx())

    assert result.abstain_reason == QUOTA_UNMET


@pytest.mark.asyncio
async def test_i3_abstains_before_searching(tmp_path: Path) -> None:
    tei = FakeTei({PROD.text: 0.9})
    svc = await service(tmp_path, [REG, PROD], tei)

    result = await svc.retrieve("What is the suicide exclusion?", ctx(fsm_state="S2"))

    assert result.abstain_reason == I3_PRODUCT_GATED
    assert tei.embedded == [] and tei.reranked == []
    assert result.audit.collections == []


@pytest.mark.asyncio
async def test_s0_outside_privacy_is_not_routed(tmp_path: Path) -> None:
    svc = await service(tmp_path, [REG], FakeTei({REG.text: 0.9}))

    result = await svc.retrieve("What is the free-look period?", ctx(fsm_state="S0"))

    assert result.abstain_reason == NOT_ROUTED


@pytest.mark.asyncio
async def test_a_pin_without_thresholds_is_unavailable(tmp_path: Path) -> None:
    svc = await service(
        tmp_path, [REG], FakeTei({REG.text: 0.9}), snapshots={"regulatory": "regulatory-test"}
    )

    with pytest.raises(RetrievalUnavailable) as failed:
        await svc.retrieve("What is the free-look period?", ctx())  # also pins product and tax

    assert failed.value.reason == "THRESHOLDS_MISSING"


@pytest.mark.asyncio
async def test_no_thresholds_file_is_unavailable(tmp_path: Path) -> None:
    svc = await service(tmp_path, [REG], FakeTei({REG.text: 0.9}))
    (tmp_path / "thresholds.yaml").unlink()
    fresh = type(svc)(svc._tei, svc._qdrant, svc._snapshot_meta, config_dir=tmp_path)

    selection = await fresh.select("What is the free-look period?", ctx())  # calibration's path
    with pytest.raises(RetrievalUnavailable) as failed:
        fresh.decide(selection)

    assert failed.value.reason == "THRESHOLDS_MISSING"


@pytest.mark.asyncio
async def test_thresholds_of_another_reranker_are_refused(tmp_path: Path) -> None:
    svc = await service(
        tmp_path,
        [REG],
        FakeTei({REG.text: 0.9}),
        reranker=TeiModel(RERANK_MODEL.model_id, "an-older-revision"),
    )

    with pytest.raises(RetrievalUnavailable) as failed:
        await svc.retrieve("What is the free-look period?", ctx())

    assert failed.value.reason == "RERANKER_MISMATCH"
