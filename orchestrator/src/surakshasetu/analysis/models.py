"""TDD §3.3, copied verbatim: the turn analyser's output shape. There is no consent intent and no
consent slot -- consent is only ever set by the Consent Service, on a structured action or a strict
parser match (never derived from analysis). test_analysis_schema.py enforces this structurally.
"""

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Intent(StrEnum):
    SLOT_ANSWER = "SLOT_ANSWER"
    CORRECTION = "CORRECTION"
    SIDE_QUERY = "SIDE_QUERY"
    META_HUMAN = "META_HUMAN"
    META_RESTART = "META_RESTART"
    META_WITHDRAW = "META_WITHDRAW"
    META_LANGUAGE = "META_LANGUAGE"
    OFF_TOPIC = "OFF_TOPIC"
    SAFETY = "SAFETY"
    ADVERSARIAL = "ADVERSARIAL"
    NEW_PURCHASE = "NEW_PURCHASE"
    SPECIFIC_PLAN = "SPECIFIC_PLAN"
    EXISTING_POLICY = "EXISTING_POLICY"
    GENERAL_FAQ = "GENERAL_FAQ"
    EXPRESS_PATH = "EXPRESS_PATH"
    NEED_TIME = "NEED_TIME"
    REJECT_ALL = "REJECT_ALL"
    DECLINE = "DECLINE"
    OBJECTION_PRICE = "OBJECTION_PRICE"
    OBJECTION_TRUST = "OBJECTION_TRUST"
    OBJECTION_COMPETITOR = "OBJECTION_COMPETITOR"
    OBJECTION_GUARANTEE = "OBJECTION_GUARANTEE"
    OBJECTION_OTHER = "OBJECTION_OTHER"
    FRUSTRATION = "FRUSTRATION"
    FINANCIAL_DISTRESS = "FINANCIAL_DISTRESS"
    COMPREHENSION_DIFFICULTY = "COMPREHENSION_DIFFICULTY"


class SlotCandidate(BaseModel):
    slot: str
    value: Any
    confidence: float = Field(ge=0, le=1)
    evidence_span: str  # the customer's exact words; nothing inferred


class TurnAnalysis(BaseModel):
    intents: list[Intent]
    slots: list[SlotCandidate] = []
    side_query: str | None = None
    language: Literal["en", "hi", "hi-Latn"]
