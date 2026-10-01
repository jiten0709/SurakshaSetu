from pathlib import Path

import pytest
from retrieval_support import FakeTei, ctx, payload, service

from surakshasetu.kb.payload import Collection, KbPayload
from surakshasetu.retrieval.gate import Scored, add_parents, parent_of, select, tokens


def chunk(domain: Collection, n: int, words: int = 10) -> KbPayload:
    text = "DUMMY: " + " ".join(f"w{n}x{i}" for i in range(words - 2))  # `words` proxy tokens
    return payload(domain, f"doc{n}", ["Root", "Doc", f"{n}. Section"], text)


def scored(domain: Collection, n: int, score: float, words: int = 10) -> Scored:
    return Scored(chunk(domain, n, words), score)


def ids(items: list[Scored]) -> list[str]:
    return [s.payload.doc_id for s in items]


def test_keep_takes_the_best_scores_in_order() -> None:
    ranked = [scored("tax", n, score=n / 10) for n in range(10)]

    assert ids(select(ranked, {}, keep=8, budget=3000)) == [f"doc{n}" for n in range(9, 1, -1)]


def test_ties_break_on_the_chunk_id() -> None:
    ranked = [scored("tax", n, score=0.5) for n in (3, 1, 2)]

    assert ids(select(ranked, {}, keep=2, budget=3000)) == ["doc1", "doc2"]


def test_a_quota_reserves_its_domains_best_chunk() -> None:
    ranked = [scored("regulatory", n, 0.9) for n in range(10)] + [scored("product", 99, 0.1)]

    kept = select(ranked, {"product": 1}, keep=8, budget=3000)

    assert len(kept) == 8
    assert "doc99" in ids(kept)  # reserved although eight regulatory chunks outscore it
    assert ids(kept)[-1] == "doc99"  # and still ranked by score


def test_the_budget_trims_the_lowest_score_first() -> None:
    ranked = [scored("tax", n, score=1 - n / 10, words=100) for n in range(8)]

    kept = select(ranked, {}, keep=8, budget=500)

    assert ids(kept) == ["doc0", "doc1", "doc2", "doc3", "doc4"]
    assert tokens(kept) == 500


def test_the_budget_never_trims_below_a_quota() -> None:
    ranked = [
        scored("regulatory", 1, 0.9, words=400),
        scored("regulatory", 2, 0.8, words=400),
        scored("product", 3, 0.1, words=400),
    ]

    assert ids(select(ranked, {"product": 1}, keep=8, budget=800)) == ["doc1", "doc3"]
    # Tighter still, even the best chunk goes before the quota's.
    assert ids(select(ranked, {"product": 1}, keep=8, budget=500)) == ["doc3"]


def test_the_budget_can_be_exceeded_only_by_quotas() -> None:
    ranked = [scored("product", 1, 0.9, words=400), scored("tax", 2, 0.8, words=400)]

    kept = select(ranked, {"product": 1, "tax": 1}, keep=8, budget=100)

    assert ids(kept) == ["doc1", "doc2"]


def _doc(section_path: list[str], text: str) -> KbPayload:
    return payload("tax", "s80c", section_path, text)


INTRO = _doc(["ITA 1961", "Section 80C"], "DUMMY: Section 80C allows a deduction.")
CLAUSE = _doc(["ITA 1961", "Section 80C", "80C(1) Limit"], "DUMMY: up to the limit.")
SUB = _doc(["ITA 1961", "Section 80C", "80C(1) Limit", "80C(1)(a) Proviso"], "DUMMY: proviso.")
OTHER_DOC = payload("tax", "s123", ["ITA 2025", "Section 123"], "DUMMY: Section 123.")


def test_the_parent_is_the_longest_proper_prefix_in_the_same_document() -> None:
    documents = [INTRO, CLAUSE, SUB, OTHER_DOC]

    assert parent_of(SUB, documents) == CLAUSE
    assert parent_of(CLAUSE, documents) == INTRO
    assert parent_of(INTRO, documents) is None


def test_a_parent_comes_from_the_same_snapshot_only() -> None:
    newer_intro = payload(
        "tax", "s80c", ["ITA 1961", "Section 80C"], INTRO.text, snapshot_id="tax-next"
    )

    assert parent_of(CLAUSE, [newer_intro]) is None


def test_parents_follow_the_kept_chunks_while_the_budget_allows() -> None:
    kept = [Scored(CLAUSE, 0.9)]

    evidence = add_parents(kept, [INTRO, CLAUSE], {INTRO.chunk_id: 0.4}, budget=3000)

    assert [(s.payload.chunk_id, s.score, s.parent) for s in evidence] == [
        (CLAUSE.chunk_id, 0.9, False),
        (INTRO.chunk_id, 0.4, True),
    ]
    assert add_parents(kept, [INTRO, CLAUSE], {}, budget=tokens(kept)) == kept
    assert add_parents(kept, [INTRO, CLAUSE], {}, budget=3000)[1].score is None  # not reranked


def test_a_parent_already_kept_is_not_added_twice() -> None:
    kept = [Scored(CLAUSE, 0.9), Scored(INTRO, 0.5)]

    assert add_parents(kept, [INTRO, CLAUSE], {}, budget=3000) == kept


@pytest.mark.asyncio
async def test_the_service_fetches_a_parent_from_outside_the_candidate_pool(
    tmp_path: Path,
) -> None:
    # One candidate per collection, and only the clause matches the query vector: the pool is the
    # clause alone, so its parent can only come from the document scroll.
    svc = await service(
        tmp_path,
        [INTRO, CLAUSE, OTHER_DOC],
        FakeTei({CLAUSE.text: 0.95}),
        vectors={CLAUSE.text: [1.0, 0.0, 0.0, 0.0]},
        limits={"per_collection": 1},
    )

    selection = await svc.select("80C limit", ctx(entities=["tax"]))

    assert selection.candidates == {"tax": 1, "product": 0}
    assert [(s.payload, s.score, s.parent) for s in selection.evidence] == [
        (CLAUSE, 0.95, False),
        (INTRO, None, True),
    ]
