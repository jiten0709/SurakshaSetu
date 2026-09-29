"""TDD §3.6: deterministic normalisers applied to slot candidates. Pure functions, no I/O."""

import re
from dataclasses import dataclass
from typing import Literal

_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")


def devanagari_digits_to_ascii(text: str) -> str:
    return text.translate(_DEVANAGARI_DIGITS)


# --- age ---------------------------------------------------------------------------------------

_AGE_YRS = re.compile(r"\b(\d{1,2})\s*(?:yrs?|years?)(?:\s*old)?\b", re.I)
_AGE_BORN = re.compile(r"\bborn in ['’]?(\d{2,4})\b", re.I)
_MIN_PLAUSIBLE_AGE = 0
_MAX_PLAUSIBLE_AGE = 120


@dataclass(frozen=True)
class AgeResult:
    value: int | None
    needs_confirmation: bool
    candidates: tuple[int, int] | None = None


def parse_age(text: str, *, current_year: int) -> AgeResult | None:
    text = devanagari_digits_to_ascii(text)
    if m := _AGE_YRS.search(text):
        return AgeResult(value=int(m.group(1)), needs_confirmation=False)
    if m := _AGE_BORN.search(text):
        digits = m.group(1)
        if len(digits) != 2:
            return AgeResult(value=current_year - int(digits), needs_confirmation=False)
        n = int(digits)
        candidates = (current_year - (1900 + n), current_year - (2000 + n))
        plausible = tuple(c for c in candidates if _MIN_PLAUSIBLE_AGE < c < _MAX_PLAUSIBLE_AGE)
        if len(plausible) == 1:
            return AgeResult(value=plausible[0], needs_confirmation=False)
        return AgeResult(value=None, needs_confirmation=True, candidates=candidates)
    return None


# --- money ---------------------------------------------------------------------------------------

_LPA = re.compile(r"(\d+(?:\.\d+)?)\s*lpa\b", re.I)
_CRORE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:cr|crore)\b", re.I)
_LAKH = re.compile(r"(\d+(?:\.\d+)?)\s*lakh[s]?\b", re.I)
_INDIAN_GROUPED = re.compile(r"\b(\d{1,2}(?:,\d{2})*,\d{3})\b")
_K_MONTHLY = re.compile(r"(\d+(?:\.\d+)?)\s*k\s*(?:a|per|/)\s*month\b", re.I)
_K_BARE = re.compile(r"\b(\d+(?:\.\d+)?)\s*k\b", re.I)

_LAKH_INR = 100_000
_CRORE_INR = 10_000_000
_THOUSAND_INR = 1_000
_MONTHS_PER_YEAR = 12


@dataclass(frozen=True)
class MoneyResult:
    annual_inr: int | None
    needs_confirmation: bool
    period_ambiguous: bool = False


def parse_money(text: str) -> MoneyResult | None:
    text = devanagari_digits_to_ascii(text)
    if m := _LPA.search(text):
        return MoneyResult(
            annual_inr=round(float(m.group(1)) * _LAKH_INR), needs_confirmation=False
        )
    if m := _CRORE.search(text):
        return MoneyResult(
            annual_inr=round(float(m.group(1)) * _CRORE_INR), needs_confirmation=False
        )
    if m := _LAKH.search(text):
        return MoneyResult(
            annual_inr=round(float(m.group(1)) * _LAKH_INR), needs_confirmation=False
        )
    if m := _INDIAN_GROUPED.search(text):
        return MoneyResult(annual_inr=int(m.group(1).replace(",", "")), needs_confirmation=False)
    if m := _K_MONTHLY.search(text):
        monthly = float(m.group(1)) * _THOUSAND_INR
        return MoneyResult(annual_inr=round(monthly * _MONTHS_PER_YEAR), needs_confirmation=True)
    if _K_BARE.search(text):
        return MoneyResult(annual_inr=None, needs_confirmation=True, period_ambiguous=True)
    return None


# --- pincode -------------------------------------------------------------------------------------

_PINCODE = re.compile(r"\b([1-9]\d{5})\b")


def parse_pincode(text: str) -> str | None:
    text = devanagari_digits_to_ascii(text)
    m = _PINCODE.search(text)
    return m.group(1) if m else None


# --- yes/no/declined ------------------------------------------------------------------------------

_YES = frozenset({"yes", "yeah", "yep", "sure", "haan", "han", "हाँ", "हां", "ji haan", "ji"})
_NO = frozenset({"no", "nope", "nahi", "nahin", "नहीं"})
_DECLINE_PHRASES = (
    "decline",
    "declined",
    "prefer not to say",
    "don't want to say",
    "rather not say",
    "nahi bataunga",
    "nahi batana",
    "nahi batauna",
    "skip this",
)


def parse_yes_no(text: str) -> Literal["yes", "no", "declined"] | None:
    folded = text.strip().casefold()
    if any(phrase in folded for phrase in _DECLINE_PHRASES):
        return "declined"
    if folded in _YES:
        return "yes"
    if folded in _NO:
        return "no"
    return None
