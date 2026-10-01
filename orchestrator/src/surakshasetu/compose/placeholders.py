"""Number placeholders (TDD §3.8): the model writes {{premium:<UIN>}}, {{cover:<UIN>}},
{{term:<UIN>}}, {{ppt:<UIN>}}, {{gap:<UIN>}} or {{need}}, and the composer fills them from the
engine's own values. Nothing else may supply a number here, so this module never imports retrieval
(tests/unit/test_retrieval_boundary.py).

term and ppt fill a bare number of years (the model writes the unit, in the customer's language);
money fills rupees with Indian digit grouping. An unknown placeholder, an option that was not
ranked, a missing value or a stray brace raises PlaceholderError, which Step 14 fails as grounding.
"""

import re
from datetime import date
from decimal import Decimal

from surakshasetu.domain.models import RankingResult, SuitabilityResult

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_GRAMMAR = re.compile(r"(premium|cover|term|ppt|gap):([0-9]{3}[A-Z][0-9]{3}V[0-9]{2})|need")


class PlaceholderError(Exception):
    """reason: UNKNOWN_PLACEHOLDER, NOT_RANKED, VALUE_MISSING or MALFORMED."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def format_inr(amount: Decimal | str) -> str:
    """₹ with Indian grouping (₹1,00,00,000); paise only when there are any."""
    value = Decimal(amount).quantize(Decimal("0.01"))
    rupees, paise = divmod(abs(value), 1)
    digits = str(int(rupees))
    head, groups = digits[:-3], [digits[-3:]]
    while head:
        groups.insert(0, head[-2:])
        head = head[:-2]
    sign = "-" if value < 0 else ""
    return f"{sign}₹{','.join(groups)}" + (f".{int(paise * 100):02d}" if paise else "")


def format_date(day: date) -> str:
    return f"{day.day} {day.strftime('%b')} {day.year}"


def fill(text: str, ranking: RankingResult, suitability: SuitabilityResult) -> tuple[str, int]:
    """The text with every placeholder filled, and how many there were."""
    options = {option.uin: option for option in ranking.options}

    def value(match: re.Match[str]) -> str:
        grammar = _GRAMMAR.fullmatch(match.group(1).strip())
        if grammar is None:
            raise PlaceholderError("UNKNOWN_PLACEHOLDER")
        kind, uin = grammar.groups()
        if kind is None:
            return format_inr(suitability.need_inr)
        option = options.get(uin)
        if option is None:
            raise PlaceholderError("NOT_RANKED")
        if kind == "premium":
            if option.quote is None:  # premium withheld, or rating down
                raise PlaceholderError("VALUE_MISSING")
            return format_inr(option.quote.annual_premium_inr)
        if kind == "cover":
            return format_inr(option.sum_assured_inr)
        if kind == "gap":
            return format_inr(option.protection_gap_inr)
        return str(option.term_years if kind == "term" else option.ppt_years)

    filled, count = _PLACEHOLDER.subn(value, text)
    if "{" in filled or "}" in filled:
        raise PlaceholderError("MALFORMED")
    return filled, count
