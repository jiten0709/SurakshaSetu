import re
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from surakshasetu.analysis.models import Intent
from surakshasetu.retrieval.rewrite import KB_CONFIG, Aliases, Lexicon, load, rewrite
from surakshasetu.retrieval.routing import (
    I3_PRODUCT_GATED,
    NOT_ROUTED,
    RouteDecision,
    RoutingTable,
    route,
)

TABLE = load(RoutingTable, KB_CONFIG / "routing.yaml")
LEXICON = load(Lexicon, KB_CONFIG / "lexicon.yaml")
ALIASES = load(Aliases, KB_CONFIG / "aliases.yaml")
SEED_KB = KB_CONFIG.parent / "seed" / "kb"


def routed(
    question: str,
    state: str = "S3",
    *,
    intents: list[Intent] | None = None,
    entities: list[str] | None = None,
    focus: bool = False,
) -> RouteDecision:
    tokens = rewrite(question, [], LEXICON, ALIASES).tokens
    return route(
        TABLE,
        fsm_state=state,
        intents=intents or [],
        entities=entities or [],
        tokens=tokens,
        product_in_focus=focus,
    )


@pytest.mark.parametrize(
    ("question", "rule", "collections", "quotas"),
    [
        (
            "What is the suicide exclusion?",
            "RT-EXCLUSION",
            ("product", "regulatory"),
            {"product": 1},
        ),
        ("atmahatya par claim milega?", "RT-EXCLUSION", ("product", "regulatory"), {"product": 1}),
        ("How does 80C work?", "RT-TAX", ("tax", "product"), {"tax": 1}),
        ("प्रीमियम पर कटौती कितनी है?", "RT-TAX", ("tax", "product"), {"tax": 1}),
        ("Can I withdraw my consent?", "RT-PRIVACY", ("regulatory",), {"regulatory": 1}),
        ("मेरे डेटा का क्या होगा?", "RT-PRIVACY", ("regulatory",), {"regulatory": 1}),
        ("How is a death claim settled?", "RT-CLAIMS", ("product", "regulatory"), {}),
        ("Which riders can I add?", "RT-PRODUCT-FEATURE", ("product",), {"product": 1}),
        ("What is the free-look period?", "RT-DEFAULT", ("regulatory", "product", "tax"), {}),
    ],
)
def test_the_seed_rules(
    question: str, rule: str, collections: tuple[str, ...], quotas: dict[str, int]
) -> None:
    assert routed(question) == RouteDecision(rule, collections, quotas, None)  # type: ignore[arg-type]


def test_a_named_plan_goes_to_the_product_collection() -> None:
    decision = routed("Tell me about it", intents=[Intent.SPECIFIC_PLAN])

    assert (decision.rule_id, decision.collections) == ("RT-PLAN", ("product",))


def test_the_callers_entities_join_the_topics() -> None:
    assert routed("How does it work?", entities=["tax"]).rule_id == "RT-TAX"


def test_s0_retrieves_only_for_privacy() -> None:
    assert routed("How is my data used?", "S0").collections == ("regulatory",)

    other = routed("What is the free-look period?", "S0")
    assert (other.rule_id, other.collections, other.abstain_reason) == (
        "RT-S0-NONE",
        (),
        NOT_ROUTED,
    )


@pytest.mark.parametrize("state", ["S1", "S2", "QUOTE_ONLY", "HUMAN_ESCALATION"])
def test_i3_keeps_the_product_collection_out_before_s3(state: str) -> None:
    default = routed("What is the free-look period?", state)
    claims = routed("How is a death claim settled?", state)

    assert (default.collections, default.abstain_reason) == (("regulatory", "tax"), None)
    assert (claims.collections, claims.abstain_reason) == (("regulatory",), None)


@pytest.mark.parametrize("question", ["What is the suicide exclusion?", "Which riders can I add?"])
def test_i3_abstains_when_the_rule_needs_a_product_chunk(question: str) -> None:
    decision = routed(question, "S2")

    assert "product" not in decision.collections
    assert decision.abstain_reason == I3_PRODUCT_GATED


def test_a_product_in_focus_lifts_the_i3_gate() -> None:
    decision = routed("What is the suicide exclusion?", "S2", focus=True)

    assert decision.collections == ("product", "regulatory")
    assert decision.abstain_reason is None


def _table(**changes: Any) -> dict[str, Any]:
    raw = yaml.safe_load((KB_CONFIG / "routing.yaml").read_text(encoding="utf-8"))
    return raw | changes  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    "broken",
    [
        _table(rules=[{"id": "A", "when": {"topics": ["nope"]}, "collections": ["tax"]}]),
        _table(rules=[{"id": "A", "when": {"topics": ["tax"]}, "collections": ["tax"]}]),
        _table(rules=[{"id": "A", "when": {}, "collections": ["tax"], "quotas": {"product": 1}}]),
        _table(rules=[{"id": "A", "when": {}, "collections": ["tax"], "quotas": {"tax": 0}}]),
        _table(rules=[{"id": "A", "when": {}, "collections": ["web"]}]),
        _table(
            limits={"per_collection": 50, "rerank_max": 120, "keep": 8, "evidence_budget_tokens": 1}
        ),
        _table(topics={"tax": ["two words"]}),
        _table(unknown=True),
    ],
)
def test_a_broken_table_is_refused(broken: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RoutingTable.model_validate(broken)


def test_precedence_follows_tdd_2_5() -> None:
    ranks = [TABLE.rank(t) for t in ("master_circular", "policy_wording", "cis", "brochure")]

    assert ranks == sorted(ranks) and len(set(ranks)) == 4
    assert TABLE.rank("something_new") > max(TABLE.precedence.values())


def test_every_seed_doc_type_has_a_precedence() -> None:
    doc_types = {
        match
        for path in SEED_KB.rglob("*.md")
        for match in re.findall(r"^doc_type: *(\w+)", path.read_text(encoding="utf-8"), re.M)
    }

    assert doc_types and doc_types <= TABLE.precedence.keys()
