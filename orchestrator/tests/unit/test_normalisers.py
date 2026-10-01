import pytest

from surakshasetu.analysis.normalisers import (
    devanagari_digits_to_ascii,
    parse_age,
    parse_money,
    parse_pincode,
    parse_yes_no,
)

CURRENT_YEAR = 2026


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("I am 34 yrs", 34),
        ("I am 34 yr", 34),
        ("34 years old", 34),
        ("I am 5 years old", 5),
        ("29 year", 29),
        ("42 years", 42),
        ("I'm 60 yrs old", 60),
    ],
)
def test_age_from_yrs_phrasing(text: str, value: int) -> None:
    result = parse_age(text, current_year=CURRENT_YEAR)
    assert result is not None
    assert result.value == value
    assert result.needs_confirmation is False


def test_born_in_two_digit_year_with_one_plausible_century() -> None:
    result = parse_age("born in '91", current_year=CURRENT_YEAR)
    assert result is not None
    assert result.value == CURRENT_YEAR - 1991
    assert result.needs_confirmation is False


def test_born_in_two_digit_year_with_two_plausible_centuries_needs_confirmation() -> None:
    result = parse_age("born in '20", current_year=CURRENT_YEAR)
    assert result is not None
    assert result.value is None
    assert result.needs_confirmation is True
    assert result.candidates == (CURRENT_YEAR - 1920, CURRENT_YEAR - 2020)


def test_born_in_four_digit_year() -> None:
    result = parse_age("born in 1991", current_year=CURRENT_YEAR)
    assert result is not None
    assert result.value == CURRENT_YEAR - 1991
    assert result.needs_confirmation is False


def test_devanagari_age_digits_are_read() -> None:
    result = parse_age("मेरी उम्र ३४ years old", current_year=CURRENT_YEAR)
    assert result is not None
    assert result.value == 34


def test_no_age_found() -> None:
    assert parse_age("I do not smoke", current_year=CURRENT_YEAR) is None


@pytest.mark.parametrize(
    ("text", "annual_inr", "needs_confirmation", "period_ambiguous"),
    [
        ("my income is 12 LPA", 1_200_000, False, False),
        ("I earn 8.5 LPA", 850_000, False, False),
        ("annual income 6 lpa", 600_000, False, False),
        ("I have 1.5 cr in savings", 15_000_000, False, False),
        ("2 crore net worth", 20_000_000, False, False),
        ("I earn 15 lakh a year", 1_500_000, False, False),
        ("income is 15 lakhs", 1_500_000, False, False),
        ("income is 15,00,000", 1_500_000, False, False),
        ("salary is 5,00,000 per year", 500_000, False, False),
        ("I make 80k a month", 960_000, True, False),
        ("earning 50k per month", 600_000, True, False),
        ("50k / month", 600_000, True, False),
        ("I make 80k", None, True, True),
    ],
)
def test_money_parsing(
    text: str, annual_inr: int | None, needs_confirmation: bool, period_ambiguous: bool
) -> None:
    result = parse_money(text)
    assert result is not None
    assert result.annual_inr == annual_inr
    assert result.needs_confirmation is needs_confirmation
    assert result.period_ambiguous is period_ambiguous


def test_no_money_found() -> None:
    assert parse_money("I do not smoke") is None


def test_devanagari_money_digits_are_read() -> None:
    result = parse_money("मेरी income १२ LPA है")
    assert result is not None
    assert result.annual_inr == 1_200_000


@pytest.mark.parametrize(
    ("text", "pincode"),
    [
        ("my pincode is 560001", "560001"),
        ("I live in 110001 New Delhi", "110001"),
        ("पिनकोड ४००००१ है", "400001"),
    ],
)
def test_pincode_extraction(text: str, pincode: str) -> None:
    assert parse_pincode(text) == pincode


def test_no_pincode_found() -> None:
    assert parse_pincode("I live nearby") is None


def test_a_leading_zero_pincode_is_not_matched() -> None:
    assert parse_pincode("012345 is not a real pincode") is None


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("yes", "yes"),
        ("Yeah", "yes"),
        ("sure", "yes"),
        ("haan", "yes"),
        ("हाँ", "yes"),
        ("ji", "yes"),
        ("no", "no"),
        ("nope", "no"),
        ("nahi", "no"),
        ("नहीं", "no"),
        ("I decline to answer", "declined"),
        ("prefer not to say", "declined"),
        ("nahi batana", "declined"),
    ],
)
def test_yes_no_declined(text: str, answer: str) -> None:
    assert parse_yes_no(text) == answer


def test_yes_no_unrecognised_text_is_none() -> None:
    assert parse_yes_no("maybe later") is None


def test_devanagari_digits_to_ascii() -> None:
    assert devanagari_digits_to_ascii("०१२३४५६७८९") == "0123456789"
