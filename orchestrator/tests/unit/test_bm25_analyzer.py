"""The BM25 analyzer shared by ingestion and queries (TDD §2.2)."""

import logging

import pytest

from surakshasetu.retrieval.bm25 import (
    ANALYZER_VERSION,
    Sparse,
    doc_vector,
    query_vector,
    term_index,
    tokenize,
)


@pytest.mark.parametrize(
    ("text", "kept"),
    [
        ("pension plans u/s 80CCC", "80ccc"),  # 80C: test_section_spellings_normalise_to_section
        ("exempt under 10(10D)", "10(10d)"),
        ("TDS under 194DA", "194da"),
        ("listed in Schedule XV", "schedule_xv"),
        ("see Sch. XV para 1(a)", "schedule_xv"),
        ("Suraksha Term Shield (999N001V02)", "999n001v02"),
        ("rider 999A007V01 attaches", "999a007v01"),
        ("clause 5.3 of the wording", "5.3"),
        ("sub-section 123(1) applies", "123(1)"),
        ("for tax year 2025-26", "2025-26"),
    ],
)
def test_identifiers_stay_single_tokens(text: str, kept: str) -> None:
    assert kept in tokenize(text)


@pytest.mark.parametrize("text", ["sec. 80C", "sec 80C", "u/s 80C", "Section 80C", "SEC.80C"])
def test_section_spellings_normalise_to_section(text: str) -> None:
    assert tokenize(text) == ["section", "80c"]


def test_sec_inside_a_word_is_left_alone() -> None:
    assert tokenize("second secure") == ["second", "secure"]


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ("1.5 cr", "15000000"),
        ("1.5cr", "15000000"),
        ("2 crore", "20000000"),
        ("15 lakh", "1500000"),
        ("1.5 lakhs", "150000"),
        ("2 lac", "200000"),
        ("15,00,000", "1500000"),
        ("1,50,000", "150000"),
        ("1,00,00,000", "10000000"),
        ("1,500,000", "1500000"),
        ("₹1,500 crore", "15000000000"),
        ("15 लाख", "1500000"),
        ("२ करोड़", "20000000"),
    ],
)
def test_indian_numbers_normalise_to_digits(text: str, number: str) -> None:
    assert tokenize(text) == [number]


def test_a_list_of_numbers_is_not_read_as_grouping() -> None:
    assert tokenize("sections 80,81") == ["sections", "80", "81"]


def test_lowercase_stopwords_and_devanagari() -> None:
    assert tokenize("What is the Free-Look period of the plan?") == [
        "free",
        "look",
        "period",
        "plan",
    ]
    assert tokenize("फ्री-लुक अवधि क्या है") == ["फ्री", "लुक", "अवधि"]
    assert tokenize("term plan mein suicide exclusion kya hai") == [
        "term",
        "plan",
        "suicide",
        "exclusion",
    ]


def test_term_index_is_crc32_and_stable() -> None:
    # Pinned: a change here re-numbers every indexed term, so it needs a new ANALYZER_VERSION.
    assert term_index("80c") == 1589930795
    assert term_index("10(10d)") == 1231838827
    assert term_index("schedule_xv") == 74158221
    assert term_index("999n001v02") == 517249949
    assert all(0 <= term_index(t) < 2**31 for t in ("section", "premium", "अवधि"))
    assert ANALYZER_VERSION


def test_document_weights_are_bm25_tf_on_a_toy_corpus() -> None:
    a = ["premium", "deduction", "premium"]
    b = ["premium"]
    avgdl = (len(a) + len(b)) / 2  # 2.0

    # k1 = 1.2, b = 0.75: norm = k1 * (1 - b + b * dl / avgdl); weight = tf * (k1 + 1) / (tf + norm)
    # a: norm = 1.2 * (0.25 + 0.75 * 1.5) = 1.65; premium 2 * 2.2 / 3.65, deduction 2.2 / 2.65
    # b: norm = 1.2 * (0.25 + 0.75 * 0.5) = 0.75; premium 2.2 / 1.75
    weights_a = dict(zip(*_unpack(doc_vector(a, avgdl)), strict=True))
    weights_b = dict(zip(*_unpack(doc_vector(b, avgdl)), strict=True))

    assert weights_a[term_index("premium")] == pytest.approx(4.4 / 3.65)
    assert weights_a[term_index("deduction")] == pytest.approx(2.2 / 2.65)
    assert weights_b == {term_index("premium"): pytest.approx(2.2 / 1.75)}


def test_vectors_have_unique_sorted_indices() -> None:
    vector = doc_vector(["x", "y", "x"], 3.0)
    assert vector.indices == sorted(set(vector.indices))
    assert doc_vector([], 3.0) == Sparse([], [])


def test_query_weights_are_one_per_distinct_term() -> None:
    vector = query_vector(tokenize("80C deduction 80C limit"))
    assert vector.indices == sorted({term_index(t) for t in ("80c", "deduction", "limit")})
    assert vector.values == [1.0, 1.0, 1.0]


def _unpack(vector: Sparse) -> tuple[list[int], list[float]]:
    return vector.indices, vector.values


def test_the_analyzer_never_logs_query_text(caplog: pytest.LogCaptureFixture) -> None:
    # Step 12 tokenizes customer queries with it: nothing may reach the logs.
    sentinel = "SENTINEL-4412 my pincode 560001 and income 15 lakh"
    with caplog.at_level(logging.DEBUG):
        query_vector(tokenize(sentinel))
        doc_vector(tokenize(sentinel), 5.0)

    assert caplog.records == []
