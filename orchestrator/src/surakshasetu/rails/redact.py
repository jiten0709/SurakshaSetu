"""Rail 2 (TDD §4.2, §3.9): PII redaction over Presidio, with custom Indian recognisers.

Two outputs: `stored_raw` (AADHAAR/CARD/BANK_ACCOUNT masked, everything else kept -- this is what
gets encrypted into TURN_INPUT and conv.turn.text_enc) and `redacted` (every entity tokenised --
conv.turn.redacted, logs, REDACTED routes). Presidio's NLP engine only has an English model, so its
NER-based recognisers (not the regex ones below) degrade on hi/hi-Latn text; the Indian recognisers
are regex-based and unaffected.
"""

import re
from dataclasses import dataclass
from functools import lru_cache

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider

NEVER_STORE = frozenset({"AADHAAR", "CARD", "BANK_ACCOUNT"})
# Exactly the task's list: our six Indian recognisers plus Presidio's built-in email one. Without
# this, Presidio's default battery (UK_NHS, US_SSN, PERSON, ORGANIZATION, ...) also fires and wins
# overlapping spans on its own scoring, well outside what TDD §4.2 asks this rail to catch.
ENTITIES = ("AADHAAR", "CARD", "PAN", "IN_MOBILE", "IFSC", "BANK_ACCOUNT", "EMAIL_ADDRESS")
_MASK = "[REDACTED]"
_BANK_ACCOUNT_MIN_SCORE = 0.5  # below this, no context word was found nearby


@dataclass(frozen=True)
class RedactResult:
    stored_raw: str
    redacted: str
    reminder: bool  # a never-store entity was found; ask the customer not to share it again


def entities(text: str) -> list[tuple[str, int, int]]:
    """(entity type, start, end) of each match, in order; an overlapping later match is dropped."""
    results = sorted(
        (
            r
            for r in _analyzer().analyze(text=text, language="en", entities=list(ENTITIES))
            if r.entity_type != "BANK_ACCOUNT" or r.score >= _BANK_ACCOUNT_MIN_SCORE
        ),
        key=lambda r: r.start,
    )
    found: list[tuple[str, int, int]] = []
    for result in results:
        if found and result.start < found[-1][2]:
            continue  # an overlapping, lower-priority match; keep the earlier one
        found.append((result.entity_type, result.start, result.end))
    return found


def redact(text: str) -> RedactResult:
    stored_raw_parts: list[str] = []
    redacted_parts: list[str] = []
    counts: dict[str, int] = {}
    reminder = False
    cursor = 0
    for entity_type, start, end in entities(text):
        gap = text[cursor:start]
        stored_raw_parts.append(gap)
        redacted_parts.append(gap)
        span = text[start:end]
        if entity_type in NEVER_STORE:
            stored_raw_parts.append(_MASK)
            reminder = True
        else:
            stored_raw_parts.append(span)
        counts[entity_type] = counts.get(entity_type, 0) + 1
        redacted_parts.append(f"<{entity_type}_{counts[entity_type]}>")
        cursor = end
    stored_raw_parts.append(text[cursor:])
    redacted_parts.append(text[cursor:])
    return RedactResult(
        stored_raw="".join(stored_raw_parts), redacted="".join(redacted_parts), reminder=reminder
    )


@lru_cache(maxsize=1)
def _analyzer() -> AnalyzerEngine:
    provider = NlpEngineProvider(
        nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        }
    )
    analyzer = AnalyzerEngine(nlp_engine=provider.create_engine(), supported_languages=["en"])
    for recognizer in (
        _AadhaarRecognizer(),
        _CardRecognizer(),
        _PanRecognizer(),
        _MobileRecognizer(),
        _IfscRecognizer(),
        _BankAccountRecognizer(),
    ):
        analyzer.registry.add_recognizer(recognizer)
    return analyzer


class _AadhaarRecognizer(PatternRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entity="AADHAAR",
            patterns=[Pattern("aadhaar", r"\b\d{4}[ -]?\d{4}[ -]?\d{4}\b", 0.4)],
        )

    def validate_result(self, pattern_text: str) -> bool | None:
        return _verhoeff_valid(re.sub(r"[ -]", "", pattern_text))


class _CardRecognizer(PatternRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entity="CARD",
            patterns=[Pattern("card", r"\b(?:\d[ -]?){13,19}\b", 0.3)],
        )

    def validate_result(self, pattern_text: str) -> bool | None:
        return _luhn_valid(re.sub(r"[ -]", "", pattern_text))


class _PanRecognizer(PatternRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entity="PAN", patterns=[Pattern("pan", r"\b[A-Z]{5}\d{4}[A-Z]\b", 0.6)]
        )


class _MobileRecognizer(PatternRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entity="IN_MOBILE",
            patterns=[Pattern("in_mobile", r"\b(?:\+91[-\s]?|0)?[6-9]\d{9}\b", 0.5)],
        )


class _IfscRecognizer(PatternRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entity="IFSC",
            patterns=[Pattern("ifsc", r"\b[A-Z]{4}0[A-Z0-9]{6}\b", 0.6)],
        )


class _BankAccountRecognizer(PatternRecognizer):
    def __init__(self) -> None:
        super().__init__(
            supported_entity="BANK_ACCOUNT",
            patterns=[Pattern("bank_account", r"\b\d{9,18}\b", 0.3)],
            context=["account", "a/c", "acc", "bank", "khata", "debit"],
        )


def _verhoeff_valid(digits: str) -> bool:
    if len(digits) != 12 or not digits.isdigit():
        return False
    checksum = 0
    for i, ch in enumerate(reversed(digits)):
        checksum = _VERHOEFF_D[checksum][_VERHOEFF_P[i % 8][int(ch)]]
    return checksum == 0


def _luhn_valid(digits: str) -> bool:
    if not 13 <= len(digits) <= 19 or not digits.isdigit():
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)
