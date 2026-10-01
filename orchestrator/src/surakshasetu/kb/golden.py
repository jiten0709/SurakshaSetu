"""The golden sets (content/golden, TDD §7.4): what kb-verify checks against an index and what the
retrieval metrics (Step 12) score. Gold ids are chunk ids of the snapshot each file names."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from surakshasetu.kb.payload import Collection

Language = Literal["en", "hi", "hi-Latn"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GoldenQuestion(_Strict):
    id: str
    question: str
    language: Language
    gold_chunk_ids: list[str] = Field(min_length=1)


class GoldenSet(_Strict):
    collection: Collection
    snapshot_id: str  # the snapshot the gold ids were read from
    questions: list[GoldenQuestion]


class UnanswerableQuestion(_Strict):
    id: str
    question: str
    language: Language


class UnanswerableSet(_Strict):
    questions: list[UnanswerableQuestion]


def load_golden(path: Path) -> GoldenSet:
    return GoldenSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_unanswerable(path: Path) -> UnanswerableSet:
    return UnanswerableSet.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
