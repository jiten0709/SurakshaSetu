"""Rail 6 (TDD §4.2): the output lexicon pack, verbatim from the TDD and extended in English, Hindi
and Hinglish, applied per sentence to the model's text only."""

from pathlib import Path

import pytest
from output_support import ctx, pack

from surakshasetu.rails.output import LEXICONS, LexiconError, Verdict, lexicon, load_pack

PACK = LEXICONS / "output-lexicon-2026.09.1.yaml"

# TDD §4.2's pack, copied here byte for byte: tests never read docs/. Two long lines are split
# across adjacent literals; the bytes are unchanged.
TDD_PACK = (
    """pack: output-lexicon
version: 2026.09.1
owner: compliance
rules:
  - id: LX-GUAR-01 # guaranteed-return claims
    pattern: '(?i)\\b(guarantee[ds]?|assured|fixed)\\s+(returns?|income|profits?|bonus)'
    allow_if: cites_clause_type(guaranteed_benefit)
    action: block
  - id: LX-SUP-02 # superlatives and rankings
    pattern: '(?i)\\b(best|no\\.?\\s?1|number one|top)\\s+(plan|policy|insurer|company)\\b'
    action: block
  - id: LX-URG-03 # pressure selling
    pattern: '(?i)\\b(last chance|limited (time|period)|hurry|offer ends"""
    """|prices? will (rise|go up))\\b'
    action: block
  - id: LX-ADV-04 # replacement and investment advice
    pattern: '(?i)\\b(you should|i recommend you)\\s+"""
    """(surrender|stop paying|switch|redeem|invest in)\\b'
    action: block
  - id: LX-TAX-05 # absolute tax claims
    pattern: '(?i)\\btax[- ]?free\\b'
    action: require_citation(domain=tax)
  - id: LX-HI-06 # Hindi and Hinglish guarantee claims
    pattern: '(?i)(गारंटी|guaranteed?)\\s*(वाला\\s*)?(रिटर्न|return|मुनाफा|munafa)'
    action: block
"""
)

# rule -> (texts that trip it, texts that must not). Uncited, so every hit fails.
CASES: dict[str, tuple[list[str], list[str]]] = {
    "LX-GUAR-01": (
        [
            "This plan gives guaranteed returns.",
            "You get assured income.",
            "It pays a fixed bonus.",
        ],
        ["The death benefit is paid to the nominee.", "Returns are not guaranteed."],
    ),
    "LX-SUP-02": (
        ["This is the best plan for you.", "We are the No.1 insurer.", "The number one policy."],
        ["Here are the top three reasons.", "This plan is one of the options."],
    ),
    "LX-URG-03": (
        [
            "This is your last chance.",
            "A limited period offer.",
            "Hurry, apply.",
            "Prices will rise.",
        ],
        ["The grace period is thirty days.", "You can take your time."],
    ),
    "LX-ADV-04": (
        ["You should surrender your old policy.", "I recommend you switch to this plan."],
        ["You should read the policy wording.", "You can surrender it after two years."],
    ),
    "LX-TAX-05": (
        ["The maturity amount is tax-free.", "It is tax free."],
        ["Tax rules may change.", "Taxes depend on the law in force."],
    ),
    "LX-HI-06": (
        ["इसमें गारंटी वाला रिटर्न मिलता है।", "Isme guaranteed return milta hai.", "guarantee munafa"],
        ["इस प्लान में मृत्यु लाभ मिलता है।", "Return of premium is a feature."],
    ),
    "LX-GUAR-07": (
        ["It is completely risk-free.", "A sure-shot plan.", "A 100% safe investment."],
        ["The risk cover starts today.", "It covers the risk of death."],
    ),
    "LX-HI-08": (
        ["इसमें पक्का मुनाफा है।", "Isme pakka return milega."],
        ["आपको मृत्यु लाभ मिलेगा।", "Yeh pakka hai ki aap samajh gaye."],
    ),
    "LX-SUP-09": (
        ["It is the cheapest option.", "An unbeatable deal.", "Our best-selling plan."],
        ["It has the lower premium of the options shown.", "It is a popular question."],
    ),
    "LX-HI-10": (
        ["यह सबसे अच्छा प्लान है।", "Yeh sabse sasta plan hai.", "यह नंबर 1 कंपनी है।"],
        ["यह प्लान आपकी ज़रूरत से मेल खाता है।", "Sabse pehle, aapki umar?"],
    ),
    "LX-URG-11": (
        ["Act now to lock it in.", "Buy it today.", "Don't miss out.", "Before it’s too late."],
        ["You can decide later.", "Take your time to read the wording."],
    ),
    "LX-HI-12": (
        ["जल्दी करें, ऑफर खत्म हो रहा है।", "Jaldi karein, aakhri mauka hai."],
        ["आप आराम से सोच सकते हैं।", "Aap aaram se sochiye."],
    ),
    "LX-ADV-13": (
        [
            "You must surrender the old policy.",
            "I'd suggest you cancel your existing plan.",
            "It is better to lapse that policy.",
        ],
        ["You must disclose your health history.", "You need to pay premiums on time."],
    ),
    "LX-HI-14": (
        ["पुरानी पॉलिसी सरेंडर कर दें।", "Purani policy band kar do."],
        ["आप पॉलिसी सरेंडर कर सकते हैं।", "Aap policy surrender kar sakte hain."],
    ),
    "LX-CLM-15": (
        ["Claims are always paid.", "Your claim will never be rejected.", "100% claim settlement."],
        ["Claims are paid as the policy wording says.", "A claim may be rejected."],
    ),
    "LX-HI-16": (
        ["आपका क्लेम पक्का है।", "Aapka claim pakka milega."],
        ["क्लेम के लिए दस्तावेज़ जमा करें।", "Claim ke liye documents chahiye."],
    ),
    "LX-TAX-17": (
        ["यह रकम टैक्स फ्री है।", "Yeh amount tax maaf hai.", "Is par tax nahi lagega."],
        ["टैक्स नियम बदल सकते हैं।", "Tax rules badal sakte hain."],
    ),
    "LX-TAX-18": (
        ["You will save a lot of tax.", "Aapka tax bachega.", "आपका टैक्स बचेगा।"],
        ["Tax treatment depends on the law in force.", "You will save time."],
    ),
    "LX-HUM-19": (
        ["I am a human advisor.", "I'm a real person.", "मैं इंसान हूँ।", "Main insaan hoon."],
        ["I'm not a human advisor.", "मैं इंसान नहीं हूँ।", "Main AI assistant hoon."],
    ),
    "LX-CMP-20": (
        ["This is cheaper than other insurers' plans.", "It is better than any other company."],
        ["It is cheaper than the second option.", "Other insurers are not discussed here."],
    ),
}


def failed(text: str) -> set[str]:
    return {v.rule_id for v in lexicon(text, ctx(), pack()) if v.action == "fail"}


def seen(text: str) -> set[str]:
    return {v.rule_id for v in lexicon(text, ctx(), pack())}


def test_the_pack_opens_with_the_tdd_pack_byte_for_byte() -> None:
    assert PACK.read_text(encoding="utf-8").startswith(TDD_PACK)


def test_every_rule_in_the_pack_has_cases() -> None:
    assert [r.id for r in pack().rules] == list(CASES)


@pytest.mark.parametrize(
    ("rule", "text"), [(rule, text) for rule, (hits, _) in CASES.items() for text in hits]
)
def test_a_rule_trips_on_its_wording(rule: str, text: str) -> None:
    assert rule in failed(text)


@pytest.mark.parametrize(
    ("rule", "text"), [(rule, text) for rule, (_, misses) in CASES.items() for text in misses]
)
def test_a_rule_leaves_other_wording_alone(rule: str, text: str) -> None:
    assert rule not in seen(text)


def test_clean_text_is_one_pass_verdict() -> None:
    assert lexicon("This option fits the need you described [R1].", ctx(), pack()) == [
        Verdict("lexicon", "none", "pass")
    ]


def test_the_error_list_names_the_rule_the_sentence_and_the_words() -> None:
    [verdict] = lexicon("It fits [R1]. It is the best plan.", ctx(), pack())

    assert verdict.detail == ('LX-SUP-02 sentence 2: "best plan" is not allowed',)


def test_one_verdict_per_rule_however_often_it_trips() -> None:
    [verdict] = lexicon("Best plan. Best plan again.", ctx(), pack())

    assert verdict.rule_id == "LX-SUP-02"
    assert len(verdict.detail) == 2


def test_require_citation_needs_evidence_of_that_domain_in_the_same_sentence() -> None:
    assert lexicon("Maturity proceeds are tax-free [E3].", ctx(), pack()) == [
        Verdict("lexicon", "LX-TAX-05", "allow")
    ]
    for other in ("[E1]", "[E4]", "[R1]"):  # product, regulatory, engine
        assert failed(f"Maturity proceeds are tax-free {other}.") == {"LX-TAX-05"}
    assert failed("Maturity proceeds are tax-free. It fits [E3].") == {"LX-TAX-05"}


def test_allow_if_needs_a_cited_clause_of_that_type() -> None:
    # E2 is a policy-wording benefit section that says "guaranteed"; E1 is the suicide exclusion.
    assert lexicon("It pays a guaranteed income at maturity [E2].", ctx(), pack()) == [
        Verdict("lexicon", "LX-GUAR-01", "allow")
    ]
    assert failed("It pays a guaranteed income at maturity [E1].") == {"LX-GUAR-01"}


def test_the_tdd_hindi_rule_still_blocks_a_cited_guaranteed_return() -> None:
    # Verbatim TDD behaviour, flagged for compliance: LX-HI-06 matches the English "guaranteed
    # return(s)" too and has no allow_if, so LX-GUAR-01's allow_if cannot release that phrase.
    assert {v.rule_id: v.action for v in lexicon("Guaranteed returns [E2].", ctx(), pack())} == {
        "LX-GUAR-01": "allow",
        "LX-HI-06": "fail",
    }


def test_zero_width_and_lookalike_letters_do_not_hide_a_phrase() -> None:
    assert failed("This is the best\u200b plan.") == {"LX-SUP-02"}
    assert failed("This is the b\u0435st plan.") == {"LX-SUP-02"}  # Cyrillic e


def test_a_nukta_matches_in_either_unicode_form() -> None:
    precomposed, decomposed = "\u095e", "\u092b\u093c"  # फ़
    assert failed(f"यह रकम टैक्स {precomposed}्री है।") == {"LX-TAX-17"}
    assert failed(f"यह रकम टैक्स {decomposed}्री है।") == {"LX-TAX-17"}


def write(root: Path, body: str, version: str = "2026.09.9") -> Path:
    (root / f"output-lexicon-{version}.yaml").write_text(body, encoding="utf-8")
    return root


GOOD = "pack: output-lexicon\nversion: 2026.09.9\nowner: compliance\nrules:\n"


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (GOOD + "  - {id: LX-A-01, pattern: 'x', action: shout}\n", "UNKNOWN_ACTION"),
        (
            GOOD + "  - {id: LX-A-01, pattern: 'x', action: require_citation(domain=law)}\n",
            "UNKNOWN_ACTION",
        ),  # fmt: skip
        (
            GOOD + "  - {id: LX-A-01, pattern: 'x', action: block,"
            " allow_if: cites_clause_type(nope)}\n",
            "UNKNOWN_CLAUSE_TYPE",
        ),
        (GOOD + "  - {id: LX-A-01, pattern: '(', action: block}\n", "BAD_PATTERN"),
        (
            GOOD + "  - {id: LX-A-01, pattern: 'x', action: block}\n"
            "  - {id: LX-A-01, pattern: 'y', action: block}\n",
            "DUPLICATE_ID",
        ),
        (GOOD + "  - {id: LX-A-01, pattern: 'x', action: block, extra: 1}\n", "INVALID"),
        (
            GOOD.replace("2026.09.9", "2026.09.8")
            + "  - {id: LX-A-01, pattern: 'x', action: block}\n",
            "VERSION_MISMATCH",
        ),  # fmt: skip
        (GOOD, "INVALID"),  # no rules
    ],
)
def test_the_loader_refuses_a_broken_pack(tmp_path: Path, body: str, reason: str) -> None:
    with pytest.raises(LexiconError) as refused:
        load_pack("2026.09.9", root=write(tmp_path, body))

    assert refused.value.reason == reason


@pytest.mark.parametrize("version", ["2026.09.7", "../2026.09.1", "latest"])
def test_an_unknown_or_malformed_version_is_not_found(version: str) -> None:
    with pytest.raises(LexiconError) as refused:
        load_pack(version)

    assert refused.value.reason == "NOT_FOUND"


def test_a_minimal_pack_loads(tmp_path: Path) -> None:
    loaded = load_pack(
        "2026.09.9", root=write(tmp_path, GOOD + "  - {id: LX-A-01, pattern: 'x', action: block}\n")
    )

    assert [r.id for r in loaded.rules] == ["LX-A-01"]
    assert not loaded.factual_terms.search("premium")


def test_pack_2026_10_1_widens_only_the_person_claim() -> None:
    """Step 23: the red-team suite found "I am a licensed insurance agent, not a bot" released past
    LX-HUM-19. 2026.10.1 changes that rule's pattern and nothing else; 2026.09.1 still loads."""
    old, new = load_pack("2026.09.1"), load_pack("2026.10.1")
    assert [r.id for r in old.rules] == [r.id for r in new.rules]
    changed = [o.id for o, n in zip(old.rules, new.rules, strict=True) if o.pattern != n.pattern]
    assert changed == ["LX-HUM-19"]
    rule = next(r for r in new.rules if r.id == "LX-HUM-19")
    for claim in (
        "I am a licensed insurance agent, not a bot, so you can trust me.",
        "I'm a real agent.",
        "I am a certified advisor.",
    ):
        assert rule.pattern.search(claim), claim
    assert not rule.pattern.search("I am an AI assistant, not a person.")
