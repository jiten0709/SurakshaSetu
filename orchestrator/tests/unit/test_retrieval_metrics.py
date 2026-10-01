from pathlib import Path
from typing import Any

import pytest
import yaml

from surakshasetu.eval.retrieval_metrics import (
    CachedRerank,
    Report,
    calibrate,
    context_precision_at_k,
    questions,
    recall_at_k,
    write_thresholds,
)
from surakshasetu.gateway import TeiModel
from surakshasetu.retrieval.gate import SnapshotThresholds, Stamp, Thresholds


def test_recall_counts_gold_chunks_in_the_top_8_only() -> None:
    ranked = [f"c{i}" for i in range(10)]

    assert recall_at_k(["c0", "c7"], ranked) == 1.0
    assert recall_at_k(["c0", "c8"], ranked) == 0.5  # c8 is ninth
    assert recall_at_k(["x"], ranked) == 0.0


def test_precision_is_rank_aware_like_ragas() -> None:
    # Relevant at ranks 1 and 3: (1/1 + 2/3) / 2.
    assert context_precision_at_k(["a", "c"], ["a", "b", "c", "d"]) == pytest.approx(5 / 6)
    # One gold chunk at rank 1 is perfect, however many others follow it.
    assert context_precision_at_k(["a"], ["a", "b", "c", "d", "e", "f", "g", "h"]) == 1.0
    # The same chunk at rank 2 halves it.
    assert context_precision_at_k(["a"], ["b", "a"]) == 0.5
    assert context_precision_at_k(["a"], ["b"] * 8 + ["a"]) == 0.0  # outside the top 8


def test_calibration_splits_answerable_from_unanswerable_in_the_widest_gap() -> None:
    assert calibrate([0.9, 0.8, 0.7], [0.2, 0.3]) == 0.5


def test_calibration_maximises_accuracy_when_the_sets_overlap() -> None:
    # Cuts at 0.3 and 0.7 each misplace one question (0.6, then 0.5); 0.3 sits in the wider gap.
    assert calibrate([0.9, 0.8, 0.5], [0.6, 0.1]) == pytest.approx(0.3)


def test_calibration_with_no_negatives_answers_every_positive() -> None:
    assert calibrate([0.4, 0.8], []) == 0.2


def test_calibration_keeps_a_cut_between_scores_when_rounding() -> None:
    assert 0.1234561 < calibrate([0.1234562], [0.1234561]) <= 0.1234562


def test_the_golden_sets_load_as_70_questions_on_their_snapshots() -> None:
    items, pins = questions()

    assert len(items) == 70
    assert sum(q.collection is None for q in items) == 10
    assert set(pins) == {"regulatory", "product", "tax"}


class _Gateway:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def rerank_model(self) -> TeiModel:
        return TeiModel("org/reranker", "r1")

    async def rerank(self, query: str, docs: list[str]) -> list[float]:
        self.calls.append(docs)
        return [len(d) / 10 for d in docs]


def _stored(directory: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in directory.iterdir())


@pytest.mark.asyncio
async def test_the_rerank_cache_asks_only_for_unseen_pairs(tmp_path: Path) -> None:
    gateway = _Gateway()
    cached = CachedRerank(gateway, tmp_path)  # type: ignore[arg-type]

    first = await cached.rerank("q", ["aa", "bbb"])
    second = await cached.rerank("q", ["bbb", "c"])
    reloaded = await CachedRerank(gateway, tmp_path).rerank("q", ["aa", "bbb", "c"])  # type: ignore[arg-type]

    assert (first, second, reloaded) == ([0.2, 0.3], [0.3, 0.1], [0.2, 0.3, 0.1])
    assert gateway.calls == [["aa", "bbb"], ["c"]]
    assert "bbb" not in _stored(tmp_path)  # hashes only


def _thresholds(sha: str, **snapshots: float) -> Thresholds:
    return Thresholds(
        degraded_factor=1.25,
        reranker=Stamp(model_id="org/reranker", model_sha=sha),
        snapshots={s: SnapshotThresholds(rerank=v, bm25=4.0) for s, v in snapshots.items()},
    )


def _load(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_writing_thresholds_keeps_older_snapshots_for_the_same_reranker(tmp_path: Path) -> None:
    path = tmp_path / "thresholds.yaml"
    report = Report(["collection line"], {}, {}, 1.0)
    write_thresholds(path, _thresholds("r1", **{"tax-old": 0.4}), report, report, {})
    write_thresholds(path, _thresholds("r1", **{"tax-new": 0.5}), report, report, {})

    assert _load(path)["snapshots"] == {
        "tax-new": {"rerank": 0.5, "bm25": 4.0},
        "tax-old": {"rerank": 0.4, "bm25": 4.0},
    }
    assert path.read_text(encoding="utf-8").startswith("# Sufficiency-gate thresholds")
    assert Thresholds.model_validate(_load(path))

    write_thresholds(path, _thresholds("r2", **{"tax-new": 0.6}), report, report, {})
    assert _load(path)["snapshots"] == {"tax-new": {"rerank": 0.6, "bm25": 4.0}}
