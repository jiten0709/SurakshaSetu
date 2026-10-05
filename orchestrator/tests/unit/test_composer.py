"""The S3 composer (TDD §3.8): parts in order, numbers from the engine, disclosures verbatim from
the registry, and a stable hash of exactly the text released."""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from compose_support import (
    ROP,
    TERM,
    bundle,
    chunk,
    disclosure_set,
    needs,
    option,
    product,
    ranking,
    suitability,
)

from surakshasetu.compose.citations import CitationError, EngineFact, issue
from surakshasetu.compose.composer import CompositionError, Rendered, compose
from surakshasetu.compose.placeholders import PlaceholderError
from surakshasetu.logging import configure_logging

HANDLES = issue([chunk("E1")], [EngineFact("FIT-01", "Suitability rules, FIT-01", {"fit": "TERM"})])
NARRATIVE = (
    f"Suraksha Term Shield covers {{{{cover:{TERM}}}}} for {{{{term:{TERM}}}}} years [R1]."
    f" It costs {{{{premium:{TERM}}}}} a year and pays the nominee [E1]."
)


def s3(**overrides: Any) -> Rendered:
    args: dict[str, Any] = {
        "locale": "en-IN",
        "needs": needs(),
        "suitability": suitability(),
        "ranking": ranking(
            option(1, TERM), option(2, ROP, cover="5000000", priced=False, gap="5000000")
        ),
        "products": {TERM: product(TERM), ROP: product(ROP)},
        "disclosure_sets": {TERM: disclosure_set(TERM), ROP: disclosure_set(ROP)},
        "handles": HANDLES,
        "narrative": NARRATIVE,
    }
    return compose(bundle(), **{**args, **overrides})


def test_parts_follow_the_tdd_order() -> None:
    rendered = s3()

    assert [name for name, _ in rendered.parts] == [
        "needs_recap",
        f"option_card:{TERM}",
        f"option_card:{ROP}",
        "comparison",
        "why_it_fits",
        f"disclosures:{TERM}",
        f"disclosures:{ROP}",
        "cta",
        "sources",
    ]
    assert rendered.text == "\n\n".join(text for _, text in rendered.parts)


def test_disclosures_are_the_registry_bodies_verbatim_and_in_order() -> None:
    rendered = s3()
    parts = dict(rendered.parts)

    for uin in (TERM, ROP):
        shown = disclosure_set(uin)
        assert parts[f"disclosures:{uin}"].split("\n")[1:] == [item.body for item in shown.items]
        assert rendered.disclosure_hashes[uin] == shown.set_sha256


def test_the_rendered_hash_is_stable_and_covers_the_exact_text() -> None:
    first, second = s3(), s3()

    assert first.rendered_sha256 == second.rendered_sha256
    assert first.rendered_sha256 == hashlib.sha256(first.text.encode("utf-8")).hexdigest()
    assert s3(narrative=NARRATIVE + " ").rendered_sha256 != first.rendered_sha256


def test_the_narrative_gets_engine_numbers_and_rendered_citations() -> None:
    rendered = s3()
    why = dict(rendered.parts)["why_it_fits"]

    assert why == (
        "Suraksha Term Shield covers ₹1,00,00,000 for 30 years [Source: Suitability rules, FIT-01]."
        " It costs ₹12,345 a year and pays the nominee"
        " [Source: SurakshaTermShield_999N001V02_PolicyWording §1]."
    )
    assert rendered.citations == {"R1": "FIT-01", "E1": "product:doc:e1:abc123"}
    assert dict(rendered.parts)["sources"].startswith(
        "Sources\nSuraksha Term Shield: Policy Wording v2"
    )


def test_without_a_narrative_the_deterministic_card_takes_its_place() -> None:
    rendered = s3(narrative=None)
    templates = bundle().templates["en-IN"]

    assert dict(rendered.parts)["why_it_fits"] == (
        f"{templates.recommendation.deterministic_card}\n{templates.scripts.advisor_offer}"
    )
    assert rendered.citations == {} and rendered.sources == []
    assert "sources" not in dict(rendered.parts)


def test_option_cards_show_engine_values_or_withhold_the_premium() -> None:
    parts = dict(s3().parts)

    assert parts[f"option_card:{TERM}"] == (
        f"Option 1: Suraksha Term Shield ({TERM})\n"
        "Cover: ₹1,00,00,000\n"
        "Policy term: 30 years\n"
        "Premium-paying term: 30 years\n"
        "Riders: None\n"
        "Indicative premium: ₹12,345 a year, including GST. Quote Q-2026-10-01-1V02,"
        " valid until 31 Oct 2026. The final premium is set after underwriting.\n"
        "Protection gap against your recommended cover: ₹0\n"
        f"Documents: Customer information sheet v2 (content/seed/kb/product/{TERM}-cis-v2.md);"
        f" Policy wording v10 (content/seed/kb/product/{TERM}-policy_wording-v10.md)"
    )
    card = parts[f"option_card:{ROP}"]
    assert "Premium not shown" in card and "₹50,00,000" in card


def test_a_placeholder_for_a_withheld_premium_fails() -> None:
    with pytest.raises(PlaceholderError, match="VALUE_MISSING"):
        s3(narrative=f"It costs {{{{premium:{ROP}}}}}.")


def test_a_foreign_handle_fails() -> None:
    with pytest.raises(CitationError):
        s3(narrative="It pays the nominee [E4].")


def test_documents_shown_are_the_newest_of_each_kind() -> None:
    shown = s3().documents_shown[TERM]
    docs = {(d.kind, d.version): d.sha256 for d in product(TERM).documents}

    assert shown == {"CIS": docs[("CIS", "v2")], "POLICY_WORDING": docs[("POLICY_WORDING", "v10")]}


def test_hi_in_takes_hi_in_disclosures_and_falls_back_to_en_in_documents() -> None:
    rendered = s3(
        locale="hi-IN",
        disclosure_sets={TERM: disclosure_set(TERM, "hi-IN"), ROP: disclosure_set(ROP, "hi-IN")},
    )

    assert rendered.documents_shown == s3().documents_shown
    with pytest.raises(CompositionError, match="DISCLOSURES_MISMATCH"):
        s3(locale="hi-IN")  # the en-IN sets: disclosures only in the session's language


def test_missing_inputs_refuse() -> None:
    no_cis = product(TERM).model_copy(update={"documents": product(TERM).documents[2:]})
    with pytest.raises(CompositionError, match="DOCUMENT_MISSING"):
        s3(products={TERM: no_cis, ROP: product(ROP)})
    with pytest.raises(CompositionError, match="DISCLOSURES_MISSING"):
        s3(disclosure_sets={TERM: disclosure_set(TERM)})
    with pytest.raises(CompositionError, match="NO_OPTIONS"):
        s3(ranking=ranking())


def test_the_comparison_rows_come_from_the_catalog() -> None:
    table = dict(s3().parts)["comparison"].split("\n")

    assert table[1] == f"| | Suraksha Term Shield ({TERM}) | Suraksha Term Shield ROP ({ROP}) |"
    assert "| Cover range | ₹25,00,000–₹10,00,00,000 | ₹25,00,000–₹2,00,00,000 |" in table
    assert "| Plan type | Term | Term with return of premium |" in table
    assert "comparison" not in dict(s3(ranking=ranking(option(1, TERM))).parts)


def test_the_needs_recap_states_the_assumptions() -> None:
    recap = dict(s3(partial_profile=True).parts)["needs_recap"]

    assert "Goals: Protect your family's income, Repay loans" in recap
    assert (
        "Cover you may need: ₹1,25,00,000. Recommended cover: ₹1,00,00,000 for 30 years." in recap
    )
    assert "income growth of 6.5% a year and a discount rate of 7% a year" in recap


def test_every_option_card_of_a_partial_profile_says_so() -> None:
    """Step 21: the label is on each card (the S2 bridge said it once already), not the recap."""
    label = bundle().templates["en-IN"].recommendation.partial_profile
    partial = dict(s3(partial_profile=True, ranking=ranking(option(1, TERM), option(2, ROP))).parts)
    full = dict(s3(ranking=ranking(option(1, TERM), option(2, ROP))).parts)

    assert partial[f"option_card:{TERM}"].endswith(label)
    assert partial[f"option_card:{ROP}"].endswith(label)
    assert label not in partial["needs_recap"]
    assert all(label not in text for text in full.values())


@pytest.mark.usefixtures("restore_logging")
def test_no_narrative_text_reaches_the_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", tmp_path)
    s3(narrative="Zqxv Sentinelson would like this [E1].")

    logged = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.iterdir())
    assert "composed S3 cited generation: 2 options, 0 placeholders, 1 citations" in logged
    assert "Sentinelson" not in logged and "12,345" not in logged
