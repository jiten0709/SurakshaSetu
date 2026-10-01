import pytest

from surakshasetu.rails.normalise import normalise


def test_zero_width_characters_are_stripped() -> None:
    text = "hel​lo‌ wo‍rld⁠ end﻿"
    assert normalise(text).text == "hello world end"


def test_homoglyphs_fold_to_latin() -> None:
    # Cyrillic а, е, о, р look identical to Latin a, e, o, p in most fonts.
    assert normalise("аеор").text == "aeop"


def test_devanagari_is_never_touched_by_folding() -> None:
    text = "मेरी उम्र 34 है"
    assert normalise(text).text == text


@pytest.mark.parametrize("token_count", [500, 501])
def test_the_token_cap_is_a_boundary(token_count: int) -> None:
    text = " ".join(["word"] * token_count)
    result = normalise(text, token_cap=500)
    assert result.token_count == token_count
    assert result.overlong == (token_count > 500)


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("I want a term plan for 25 lakh cover.", "en"),
        ("What is the claim process for this policy?", "en"),
        ("How much premium will I pay each year?", "en"),
        ("Please tell me about the riders available.", "en"),
        ("I would like to speak to a human agent.", "en"),
        ("मुझे 25 लाख का टर्म प्लान चाहिए", "hi"),  # no Latin letters: every such text is "hi"
        ("mujhe 25 lakh ka term plan chahiye", "hi-Latn"),
        ("meri age 34 hai aur main tobacco nahi leta", "hi-Latn"),
        ("premium kitna hoga is policy ka", "hi-Latn"),
        ("kya beema tax free hai", "hi-Latn"),
        ("aap mujhe insurance ke baare mein batao", "hi-Latn"),
    ],
)
def test_language_id(text: str, language: str) -> None:
    assert normalise(text).language == language
