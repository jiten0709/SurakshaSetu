"""Numbers reach the narrative only through placeholders filled from engine values (TDD §3.8)."""

from datetime import date

import pytest
from compose_support import ROP, TERM, option, ranking, suitability

from surakshasetu.compose.placeholders import PlaceholderError, fill, format_date, format_inr

RANKED = ranking(option(1, TERM, gap="2500000"), option(2, ROP, cover="5000000", priced=False))


@pytest.mark.parametrize(
    ("amount", "shown"),
    [
        ("10000000", "₹1,00,00,000"),
        ("2500000.50", "₹25,00,000.50"),
        ("100000.00", "₹1,00,000"),
        ("12345.6", "₹12,345.60"),
        ("999", "₹999"),
        ("0", "₹0"),
        ("-2500000", "-₹25,00,000"),
    ],
)
def test_money_uses_indian_digit_grouping(amount: str, shown: str) -> None:
    assert format_inr(amount) == shown


def test_dates_read_day_month_year() -> None:
    assert format_date(date(2026, 10, 31)) == "31 Oct 2026"


def test_every_placeholder_fills_from_the_engine_result() -> None:
    text = (
        f"Premium {{{{premium:{TERM}}}}}, cover {{{{cover:{TERM}}}}} for {{{{term:{TERM}}}}}"
        f" years, paying for {{{{ppt:{TERM}}}}} years; gap {{{{gap:{TERM}}}}}; need {{{{need}}}}."
    )

    filled, count = fill(text, RANKED, suitability("12500000.00"))

    assert filled == (
        "Premium ₹12,345, cover ₹1,00,00,000 for 30 years, paying for 30 years;"
        " gap ₹25,00,000; need ₹1,25,00,000."
    )
    assert count == 6


def test_text_without_placeholders_passes_unchanged() -> None:
    assert fill("No numbers here [E1].", RANKED, suitability()) == ("No numbers here [E1].", 0)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        (f"{{{{price:{TERM}}}}}", "UNKNOWN_PLACEHOLDER"),
        ("{{premium}}", "UNKNOWN_PLACEHOLDER"),
        (f"{{{{need:{TERM}}}}}", "UNKNOWN_PLACEHOLDER"),
        ("{{premium:999N010V01}}", "NOT_RANKED"),
        (f"{{{{premium:{ROP}}}}}", "VALUE_MISSING"),  # premium withheld
        (f"{{{{premium:{TERM}}}", "MALFORMED"),
        (f"{{premium:{TERM}}}", "MALFORMED"),
        ("a stray } brace", "MALFORMED"),
    ],
)
def test_anything_but_a_known_placeholder_with_a_value_fails(text: str, reason: str) -> None:
    with pytest.raises(PlaceholderError) as caught:
        fill(text, RANKED, suitability())

    assert caught.value.reason == reason
