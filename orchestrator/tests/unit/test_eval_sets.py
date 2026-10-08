"""The evaluation's sets (Step 23) validate offline, at their floors: at least 200 labelled slot
utterances and 150 intent turns, in English, Hindi and Hinglish, every critical intent at least 20
times, every required slot kind labelled; at least 150 red-team attack turns, each category in all
three languages; conversation and red-team ids unique across both. `make seed-eval` runs this file
with the golden assertions."""

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
from harness import load_conversations, load_redteam  # noqa: E402

from surakshasetu.eval import labels  # noqa: E402
from surakshasetu.eval.metrics import CRITICAL_INTENTS, LANGUAGES  # noqa: E402


def test_slot_labels_meet_their_floor_in_three_languages() -> None:
    items = labels.load_slots()
    assert len(items) >= 200
    by_language = Counter(i.language for i in items)
    assert set(by_language) == set(LANGUAGES) and min(by_language.values()) >= 50
    labelled = {slot for i in items for slot in i.gold}
    for needed in ("age_years", "pincode", "tobacco_12m", "dependants", "liabilities"):
        assert needed in labelled, needed
    money = {slot for slot in labelled if slot.endswith(("_inr", "_inr_pa"))}
    assert len(money) >= 5  # income, budget, premiums, covers, assets, Quote-Only's cover
    assert any(i.state == "QUOTE_ONLY" for i in items) and any(not i.gold for i in items)


def test_intent_labels_meet_their_floor_with_every_critical_intent() -> None:
    items = labels.load_intents()
    assert len(items) >= 150
    assert set(Counter(i.language for i in items)) == set(LANGUAGES)
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        for intent in item.intents:
            counts[intent.value][item.language] += 1
    for intent in CRITICAL_INTENTS:
        assert sum(counts[intent].values()) >= 20, intent
        assert set(counts[intent]) == set(LANGUAGES), intent


def test_the_red_team_suite_covers_every_category_in_three_languages() -> None:
    attacks = [t.attack for c in load_redteam() for t in c.turns if t.attack]
    assert len(attacks) >= 150
    languages: dict[str, set[str]] = defaultdict(set)
    for a in attacks:
        languages[a.category].add(a.language)
    assert all(found == set(LANGUAGES) for found in languages.values()), languages
    required = {
        "injection_in_answer", "fake_system", "user_input_breakout", "disclosure_removal",
        "guarantee", "superlative", "advice", "replacement", "uin_leak", "cross_session",
        "consent_bypass", "pii_exfiltration", "homoglyph_smuggling", "overlong", "roleplay",
        "ack_forgery", "prompt_extraction", "side_query_injection", "guarantee_objection",
    }  # fmt: skip
    assert required <= set(languages), required - set(languages)


def test_conversation_ids_are_unique_across_the_golden_and_red_team_suites() -> None:
    ids = [c.id for c in load_conversations()] + [c.id for c in load_redteam()]
    assert len(ids) == len(set(ids))
