"""BM25 sparse vectors for the knowledge base (TDD §2.2), shared by ingestion and queries.

The collections' sparse vector has Modifier.IDF, so Qdrant supplies IDF at query time: a document
vector carries only BM25's term-frequency part, and a query weighs every term 1.0. Any change to the
analyzer re-numbers or re-splits indexed terms: bump ANALYZER_VERSION and index a new snapshot.
Pure and deterministic; never logs (queries are customer text).
"""

import re
import unicodedata
import zlib
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from surakshasetu.analysis.normalisers import devanagari_digits_to_ascii

ANALYZER_VERSION = "bm25-2026.09.1"
K1 = 1.2
B = 0.75

_SECTION = re.compile(r"\bu/s\b|\bsec\b\.?")
# Digit groups first ("1,500 crore"), then scale words: western grouping, then Indian. Only
# groupings that end in three digits count, so "80,81" stays two numbers.
_GROUPED = re.compile(r"\b\d{1,3}(?:,\d{3}){2,}\b|\b\d{1,3}(?:,\d{2})*,\d{3}\b")
_CRORE = re.compile(
    unicodedata.normalize("NFKC", "(\\d+(?:\\.\\d+)?)\\s*(?:crores?\\b|cr\\b|करोड़)")
)
_LAKH = re.compile("(\\d+(?:\\.\\d+)?)\\s*(?:lakhs?\\b|lacs?\\b|लाख)")
_SCHEDULE = re.compile(r"\b(?:schedule|sch)\.?\s+([ivxlcdm]+)\b")
# Tax years, then identifiers with parentheses (10(10d), 123(1)), dotted section numbers (5.3), and
# words: letters, digits and underscores (80ccc, 999n001v02, schedule_xv) or a Devanagari run.
_TOKEN = re.compile(r"\d{4}-\d{2}\b|\d+[a-z]*(?:\([0-9a-z]+\))+|\d+(?:\.\d+)+|[a-z0-9_]+|[ऀ-ॿ]+")
_STOPWORDS_EN = (
    "a an and are as at be by can do does for from has have how i if in is it its me my of on or"
    " our so that the this to was we were what when which who will with you your rs inr"
)
_STOPWORDS_HI_LATN = "ka ki ke hai hain kya mein se ko aur bhi ho tha"
_STOPWORDS_HI = "है हैं का की के में से को और भी क्या हो था"
STOPWORDS = frozenset(
    unicodedata.normalize(
        "NFKC", " ".join((_STOPWORDS_EN, _STOPWORDS_HI_LATN, _STOPWORDS_HI))
    ).split()
)


@dataclass(frozen=True)
class Sparse:
    indices: list[int]
    values: list[float]


def tokenize(text: str) -> list[str]:
    text = devanagari_digits_to_ascii(unicodedata.normalize("NFKC", text)).lower()
    text = _SECTION.sub(" section ", text)
    text = _GROUPED.sub(lambda m: m.group().replace(",", ""), text)
    text = _CRORE.sub(lambda m: _scaled(m.group(1), 10_000_000), text)
    text = _LAKH.sub(lambda m: _scaled(m.group(1), 100_000), text)
    text = _SCHEDULE.sub(r" schedule_\1 ", text)
    return [t for t in _TOKEN.findall(text) if t not in STOPWORDS]


def term_index(term: str) -> int:
    return zlib.crc32(term.encode()) & 0x7FFFFFFF


def doc_vector(tokens: Sequence[str], avgdl: float) -> Sparse:
    """BM25 term frequency against the collection snapshot's average document length. Terms whose
    crc32 collide share one index, so their counts add."""
    if not tokens:
        return Sparse([], [])
    tf = Counter(term_index(t) for t in tokens)
    norm = K1 * (1 - B + B * len(tokens) / avgdl)
    indices = sorted(tf)
    return Sparse(indices, [tf[i] * (K1 + 1) / (tf[i] + norm) for i in indices])


def query_vector(tokens: Sequence[str]) -> Sparse:
    indices = sorted({term_index(t) for t in tokens})
    return Sparse(indices, [1.0] * len(indices))


def _scaled(number: str, scale: int) -> str:
    return f" {int(Decimal(number) * scale)} "
