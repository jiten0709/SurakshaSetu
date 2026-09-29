"""Structure-aware chunking with deterministic breadcrumbs (TDD §2.2)."""

import pytest

from surakshasetu.kb.chunker import (
    SIZE_BOUNDS,
    Block,
    ChunkingError,
    chunk_document,
    count_tokens,
    section_id,
)

ROOT = ("Suraksha Term Shield (999N001V02)", "Policy Wording v2")


def h(level: int, text: str) -> Block:
    return Block(level, text)


def p(text: str) -> Block:
    return Block(None, text)


def words(n: int, tag: str = "w") -> str:
    return "DUMMY: " + " ".join(f"{tag}{i}" for i in range(n))


def test_breadcrumbs_follow_the_heading_tree_and_drop_the_title() -> None:
    chunks = chunk_document(
        [
            h(1, "Policy Wording"),
            h(2, "5. Exclusions"),
            p(words(30, "x")),
            h(3, "5.3 Suicide"),
            p(words(30, "s")),
            h(2, "6. Free-look"),
            p(words(30, "f")),
        ],
        root=ROOT,
        bounds=(10, 100),
    )

    assert [(c.section_id, c.section_path) for c in chunks] == [
        ("5", (*ROOT, "5. Exclusions")),
        ("5.3", (*ROOT, "5. Exclusions", "5.3 Suicide")),
        ("6", (*ROOT, "6. Free-look")),
    ]
    assert " › ".join(chunks[1].section_path) == (
        "Suraksha Term Shield (999N001V02) › Policy Wording v2 › 5. Exclusions › 5.3 Suicide"
    )
    assert all(c.text.startswith("DUMMY:") for c in chunks)


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("5.3 Suicide", "5.3"),
        ("5. Exclusions", "5"),
        ("123(1) Deduction", "123(1)"),
        ("10(10D) Sums received", "10(10D)"),
        ("Schedule XV", "schedule-xv"),
        ("Key Features", "key-features"),
    ],
)
def test_section_ids_come_from_the_heading_number(heading: str, expected: str) -> None:
    assert section_id(heading) == expected


def test_an_explanation_heading_stays_with_its_parent_clause() -> None:
    chunks = chunk_document(
        [
            h(2, "3. Free-look"),
            h(3, "3.2 Period"),
            p(words(20)),
            h(4, "Explanation"),
            p("Explanation.— the period runs from receipt of the policy document."),
            h(3, "3.3 Refund"),
            p(words(20)),
        ],
        root=ROOT,
        bounds=(1, 100),
    )

    assert [c.section_id for c in chunks] == ["3.2", "3.3"]
    assert "Explanation.— the period runs" in chunks[0].text


def test_a_split_never_starts_with_a_proviso() -> None:
    clause = words(40, "a")
    proviso = "Provided that " + " ".join(f"p{i}" for i in range(30))
    chunks = chunk_document(
        [h(2, "7. Revival"), p(words(40, "z")), p(clause), p(proviso)], root=ROOT, bounds=(1, 100)
    )

    assert len(chunks) == 2
    assert chunks[1].text == f"{clause}\n\n{proviso}"
    assert all(not c.text.startswith("Provided") for c in chunks)
    assert all(count_tokens(c.text) <= 100 for c in chunks)


def test_an_oversized_section_splits_at_paragraphs_within_max() -> None:
    chunks = chunk_document(
        [h(2, "2. Benefits"), *(p(words(35, str(i))) for i in range(6))],
        root=ROOT,
        bounds=(1, 100),
    )

    assert len(chunks) > 1
    assert {c.section_id for c in chunks} == {"2"}
    assert all(count_tokens(c.text) <= 100 for c in chunks)
    assert "\n\n".join(c.text for c in chunks).count("DUMMY:") == 6


def test_small_children_merge_into_their_parent_when_it_fits() -> None:
    chunks = chunk_document(
        [
            h(2, "4. Waiting periods"),
            p(words(10, "w")),
            h(3, "4.1 Critical illness"),
            p(words(10, "c")),
            h(3, "4.2 Accident"),
            p(words(10, "a")),
        ],
        root=ROOT,
        bounds=(30, 100),
    )

    assert [(c.section_id, c.section_path) for c in chunks] == [
        ("4", (*ROOT, "4. Waiting periods"))
    ]
    assert "4.1 Critical illness" in chunks[0].text
    assert chunks[0].text.startswith("DUMMY:")


def test_large_children_stay_separate() -> None:
    chunks = chunk_document(
        [
            h(2, "4. Waiting"),
            p(words(40)),
            h(3, "4.1 A"),
            p(words(40)),
            h(3, "4.2 B"),
            p(words(40)),
        ],
        root=ROOT,
        bounds=(30, 100),
    )

    assert [c.section_id for c in chunks] == ["4", "4.1", "4.2"]


def test_a_paragraph_over_max_fails_naming_the_section() -> None:
    with pytest.raises(ChunkingError, match="5. Exclusions"):
        chunk_document([h(2, "5. Exclusions"), p(words(200))], root=ROOT, bounds=(1, 100))


def test_text_before_the_first_section_is_a_preamble() -> None:
    chunks = chunk_document([h(1, "Title"), p(words(5))], root=ROOT, bounds=(1, 100))

    assert [(c.section_id, c.section_path) for c in chunks] == [("preamble", ROOT)]


def test_size_bounds_are_the_tdd_targets() -> None:
    assert SIZE_BOUNDS == {"regulatory": (150, 450), "product": (250, 600), "tax": (150, 400)}
