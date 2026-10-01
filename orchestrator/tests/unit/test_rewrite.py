import pytest
from pydantic import ValidationError

from surakshasetu.retrieval.rewrite import KB_CONFIG, Aliases, Lexicon, load, rewrite

LEXICON = load(Lexicon, KB_CONFIG / "lexicon.yaml")
ALIASES = load(Aliases, KB_CONFIG / "aliases.yaml")


def written(query: str, focus: list[str] | None = None):  # type: ignore[no-untyped-def]
    return rewrite(query, focus or [], LEXICON, ALIASES)


@pytest.mark.parametrize(
    "query",
    [
        "Does this plan cover suicide?",
        "Does THIS PLAN cover suicide?",
        "yeh plan suicide cover karta hai?",
        "iss plan mein suicide cover hai?",
        "क्या इस प्लान में आत्महत्या कवर है?",
    ],
)
def test_references_resolve_to_the_uin_in_focus(query: str) -> None:
    result = written(query, ["999N001V02"])

    assert "999N001V02" in result.semantic
    assert "999n001v02" in result.tokens


def test_references_stay_when_nothing_is_in_focus() -> None:
    assert written("Does this plan cover suicide?").semantic == "Does this plan cover suicide?"


def test_a_reference_names_every_uin_in_focus() -> None:
    result = written("compare this plan", ["999N001V02", "999N002V01"])

    assert result.semantic == "compare 999N001V02 999N002V01"


def test_a_reference_matches_whole_words_only() -> None:
    # "is plan" is inside "this plan" and "basis plan": neither may be cut into.
    assert written("this plan", ["999N001V02"]).semantic == "999N001V02"
    assert written("the basis planning", ["999N001V02"]).semantic == "the basis planning"


def test_80c_expands_to_section_123_and_schedule_xv() -> None:
    # Aliases match analyzer tokens; test_bm25_analyzer covers the u/s and sec. spellings of 80C.
    result = written("How does 80C work?")

    assert result.semantic.endswith("(section 123; Schedule XV)")
    assert {"123", "schedule_xv"} <= set(result.tokens)


def test_115bac_expands_to_section_202() -> None:
    result = written("Which section replaced section 115BAC?")

    assert result.semantic.endswith("(section 202)")
    assert "202" in result.tokens


def test_other_sections_are_not_aliased() -> None:
    # 80CCC is one analyzer token, not 80C; its successor is tax advisory's to add.
    assert written("section 80CCC pension").semantic == "section 80CCC pension"


def test_the_alias_file_holds_only_the_tdd_stated_aliases() -> None:
    assert ALIASES.aliases == {
        "80c": ["section 123", "Schedule XV"],
        "115bac": ["section 202"],
    }


def test_hinglish_gains_english_terms_for_the_lexical_query() -> None:
    query = "atmahatya ka exclusion kitne saal ka hai?"
    result = written(query)

    assert result.semantic == query  # dense search reads the original
    assert result.lexical == f"{query} suicide years"
    assert {"suicide", "years"} <= set(result.tokens)


def test_hindi_gains_english_terms_for_the_lexical_query() -> None:
    query = "क्या नई कर व्यवस्था में जीवन बीमा प्रीमियम पर कटौती मिलती है?"
    result = written(query)

    assert result.semantic == query
    assert {"new", "regime", "life", "insurance", "premium", "deduction"} <= set(result.tokens)
    assert "tax" not in result.tokens  # कर is also "do": left out on purpose


def test_english_is_left_alone() -> None:
    query = "What is the free-look period?"
    assert written(query).lexical == query


def test_the_rewrite_is_deterministic() -> None:
    query = "yeh plan ka 80C benefit aur मृत्यु claim?"
    assert written(query, ["999N001V02"]) == written(query, ["999N001V02"])


@pytest.mark.parametrize(
    "terms",
    [{"two words": "x"}, {"hai": "is"}, {"Dawa": "claim", "dawa": "claim"}],
)
def test_a_lexicon_key_must_be_one_analyzer_token(terms: dict[str, str]) -> None:
    # Two tokens, a stopword (no token at all), and two keys that tokenize alike.
    with pytest.raises(ValidationError):
        Lexicon(references=["this plan"], terms=terms)
