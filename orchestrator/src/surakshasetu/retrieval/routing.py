"""Collection routing (TDD §2.4 step 2) from content/kb/routing.yaml: fsm_state, intents and topics
pick a rule, first match wins; then I3 keeps kb_product out before S3 unless a product is in focus.
Pure and deterministic."""

from collections.abc import Collection as Many
from dataclasses import dataclass
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from surakshasetu.analysis.models import Intent
from surakshasetu.kb.payload import COLLECTIONS, Collection
from surakshasetu.retrieval.rewrite import analyzer_token

I3_PRODUCT_GATED = "I3_PRODUCT_GATED"
NOT_ROUTED = "NOT_ROUTED"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Limits(_Strict):
    per_collection: int = Field(ge=1)
    rerank_max: int = Field(ge=1)
    keep: int = Field(ge=1)
    evidence_budget_tokens: int = Field(ge=1)

    @model_validator(mode="after")
    def _every_candidate_is_reranked(self) -> Self:
        # So no collection's candidates are cut before the rerank puts them on one scale.
        if self.per_collection * len(COLLECTIONS) > self.rerank_max:
            raise ValueError("per_collection x 3 collections exceeds rerank_max")
        return self


class When(_Strict):
    fsm_state: list[str] = []
    intents: list[Intent] = []
    topics: list[str] = []


class Rule(_Strict):
    id: str
    when: When
    collections: list[Collection]
    quotas: dict[Collection, int] = {}

    @model_validator(mode="after")
    def _quotas_are_routed(self) -> Self:
        if any(domain not in self.collections or n < 1 for domain, n in self.quotas.items()):
            raise ValueError(f"{self.id}: a quota must be at least 1 on a routed collection")
        return self


class RoutingTable(_Strict):
    limits: Limits
    product_states: list[str]  # I3: kb_product without a product in focus
    precedence: dict[str, int]  # doc_type -> TDD §2.5 rank, 1 highest
    topics: dict[str, frozenset[str]]  # topic -> analyzer tokens
    rules: list[Rule] = Field(min_length=1)

    @field_validator("topics", mode="before")
    @classmethod
    def _topic_words_are_tokens(cls, topics: dict[str, list[str]]) -> dict[str, frozenset[str]]:
        return {
            topic: frozenset(analyzer_token(w) for w in words) for topic, words in topics.items()
        }

    @model_validator(mode="after")
    def _rules_are_complete(self) -> Self:
        unknown = {t for r in self.rules for t in r.when.topics} - self.topics.keys()
        if unknown:
            raise ValueError(f"rules name unknown topics {sorted(unknown)}")
        if self.rules[-1].when != When():
            raise ValueError("the last rule must match everything")
        if len({r.id for r in self.rules}) != len(self.rules):
            raise ValueError("rule ids must be unique")
        return self

    def rank(self, doc_type: str) -> int:
        return self.precedence.get(doc_type, max(self.precedence.values(), default=0) + 1)


@dataclass(frozen=True)
class RouteDecision:
    rule_id: str
    collections: tuple[Collection, ...]
    quotas: dict[Collection, int]
    abstain_reason: str | None  # set when the question must not be searched at all


def route(
    table: RoutingTable,
    *,
    fsm_state: str,
    intents: Many[Intent],
    entities: Many[str],
    tokens: Many[str],
    product_in_focus: bool,
) -> RouteDecision:
    words = set(tokens)
    topics = {topic for topic, keys in table.topics.items() if keys & words} | set(entities)
    rule = next(r for r in table.rules if _matches(r.when, fsm_state, set(intents), topics))
    collections = tuple(rule.collections)
    reason = None if collections else NOT_ROUTED
    if "product" in collections and fsm_state not in table.product_states and not product_in_focus:
        collections = tuple(c for c in collections if c != "product")
        if not collections or "product" in rule.quotas:
            reason = I3_PRODUCT_GATED
    return RouteDecision(rule.id, collections, dict(rule.quotas), reason)


def _matches(when: When, fsm_state: str, intents: set[Intent], topics: set[str]) -> bool:
    return (
        (not when.fsm_state or fsm_state in when.fsm_state)
        and (not when.intents or bool(intents & set(when.intents)))
        and (not when.topics or bool(topics & set(when.topics)))
    )
