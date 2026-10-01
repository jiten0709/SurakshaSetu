"""Query rewrite (TDD §2.4 step 1). Pure and deterministic; never logs (queries are customer text).

- "this plan" / "yeh plan" / "इस प्लान" becomes the UIN(s) in focus;
- an alias (content/kb/aliases.yaml, owned by tax advisory) appends its expansions;
- Hindi and Hinglish words (content/kb/lexicon.yaml) append English terms, to the lexical (BM25)
  query only: dense search and rerank read the original, which the multilingual models understand.
"""

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from surakshasetu.retrieval import bm25

# The repository's content/kb, next to content/seed/kb and content/golden.
KB_CONFIG = Path(__file__).resolve().parents[4] / "content" / "kb"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def analyzer_token(key: str) -> str:
    """A config key as the one BM25 token it must be, so config matches what queries tokenize to."""
    tokens = bm25.tokenize(key)
    if len(tokens) != 1:
        raise ValueError(f"{key!r} is not exactly one analyzer token (got {tokens})")
    return tokens[0]


def _by_token[V](entries: dict[str, V]) -> dict[str, V]:
    normalised: dict[str, V] = {}
    for key, value in entries.items():
        token = analyzer_token(key)
        if token in normalised:
            raise ValueError(f"{key!r} repeats the token {token!r}")
        normalised[token] = value
    return normalised


class Aliases(_Strict):
    aliases: dict[str, list[str]]  # analyzer token -> the sections it also goes by

    @field_validator("aliases")
    @classmethod
    def keys_are_tokens(cls, aliases: dict[str, list[str]]) -> dict[str, list[str]]:
        return _by_token(aliases)


class Lexicon(_Strict):
    references: list[str] = Field(min_length=1)  # phrases meaning "the plan in focus"
    terms: dict[str, str]  # analyzer token -> English words

    @field_validator("terms")
    @classmethod
    def keys_are_tokens(cls, terms: dict[str, str]) -> dict[str, str]:
        return _by_token(terms)


@dataclass(frozen=True)
class Rewrite:
    semantic: str  # dense search: the resolved question plus alias expansions
    lexical: str  # BM25 and rerank: semantic plus the lexicon's English terms
    tokens: tuple[str, ...]  # the lexical query through the BM25 analyzer


def load[M: BaseModel](model: type[M], path: Path) -> M:
    return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def rewrite(query: str, focus_uins: list[str], lexicon: Lexicon, aliases: Aliases) -> Rewrite:
    resolved = unicodedata.normalize("NFKC", query)
    if focus_uins:
        resolved = _references(tuple(lexicon.references)).sub(" ".join(focus_uins), resolved)
    tokens = bm25.tokenize(resolved)
    expansions = _unique(
        e for key, found in aliases.aliases.items() if key in tokens for e in found
    )
    semantic = f"{resolved} ({'; '.join(expansions)})" if expansions else resolved
    english = _unique(lexicon.terms[t] for t in tokens if t in lexicon.terms)
    lexical = " ".join([semantic, *english])
    return Rewrite(semantic, lexical, tuple(bm25.tokenize(lexical)))


@cache
def _references(phrases: tuple[str, ...]) -> re.Pattern[str]:
    # Devanagari vowel signs are not \w, so word edges are spelled out: no letter, digit or
    # Devanagari character on either side.
    alternatives = "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True))
    return re.compile(f"(?<![\\wऀ-ॿ])(?:{alternatives})(?![\\wऀ-ॿ])", re.IGNORECASE)


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))
