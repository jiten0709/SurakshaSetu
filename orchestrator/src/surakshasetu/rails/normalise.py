"""Rail 1 (TDD §4.2): Unicode normalisation, zero-width/homoglyph stripping, the token cap and
language ID. Pure, deterministic, no I/O.
"""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

ZERO_WIDTH = re.compile("[​-‍⁠﻿]")
TOKEN_RE = re.compile(r"\w+|[^\w\s]", re.UNICODE)
DEVANAGARI_RE = re.compile("[ऀ-ॿ]")

# A small table, Latin lookalikes only. Devanagari is never in here, by construction.
CONFUSABLES: dict[str, str] = {
    # Cyrillic -> Latin
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X",
    # Step 23 (red-team): Cyrillic і ј ѕ һ ԁ ӏ and І Ј Ѕ, Latin dotless ı
    "і": "i", "ј": "j", "ѕ": "s", "һ": "h", "ԁ": "d", "ӏ": "l",
    "І": "I", "Ј": "J", "Ѕ": "S", "ı": "i",
    # Greek -> Latin (Step 23 adds ι, κ, ν)
    "α": "a", "ο": "o", "ρ": "p", "υ": "y", "ι": "i", "κ": "k", "ν": "v",
    "Α": "A", "Β": "B", "Ε": "E", "Ζ": "Z", "Η": "H", "Ι": "I", "Κ": "K",
    "Μ": "M", "Ν": "N", "Ο": "O", "Ρ": "P", "Τ": "T", "Υ": "Y", "Χ": "X",
}  # fmt: skip

HINGLISH_WORDS = frozenset({
    "hai", "hain", "hoon", "hun", "ho", "tha", "thi",
    "kya", "kyun", "kyu", "kaise", "kab", "kaha", "kahan", "kitna", "kitni",
    "nahi", "nahin", "haan", "han", "ji",
    "mera", "meri", "mere", "mujhe", "muje", "hum", "humein", "aap", "tum", "tumhara",
    "chahiye", "batao", "bataiye", "theek", "thik", "accha", "acha",
    "paisa", "paise", "rupaye", "beema",
})  # fmt: skip

LANGUAGE_DEVANAGARI_RATIO = 0.3


@dataclass(frozen=True)
class NormaliseResult:
    text: str
    language: Literal["en", "hi", "hi-Latn"]
    overlong: bool
    token_count: int


def normalise(raw: str, *, token_cap: int = 500) -> NormaliseResult:
    text = unicodedata.normalize("NFKC", raw)
    text = ZERO_WIDTH.sub("", text)
    text = "".join(CONFUSABLES.get(ch, ch) for ch in text)
    token_count = len(TOKEN_RE.findall(text))
    return NormaliseResult(
        text=text,
        language=_identify_language(text),
        overlong=token_count > token_cap,
        token_count=token_count,
    )


def _identify_language(text: str) -> Literal["en", "hi", "hi-Latn"]:
    letters = [ch for ch in text if ch.isalpha()]
    if letters and len(DEVANAGARI_RE.findall(text)) / len(letters) > LANGUAGE_DEVANAGARI_RATIO:
        return "hi"
    words = set(re.findall(r"[a-zA-Z]+", text.lower()))
    if words & HINGLISH_WORDS:
        return "hi-Latn"
    return "en"
