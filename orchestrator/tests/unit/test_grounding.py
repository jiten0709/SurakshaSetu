"""Rail 7 (TDD §4.2, §2.5, §3.8; I3): products, placeholders, numbers, citation handles, factual
sentences cited, and claim-level NLI through verify-claims."""

from typing import Any
from uuid import uuid4

import pytest
from compose_support import option, ranking, suitability
from output_support import ROP, TERM, Models, ctx, gateway, handles, numbers, pack, settings

from surakshasetu.compose.citations import EngineFact, TurnHandles, issue
from surakshasetu.gateway import Route
from surakshasetu.rails.output import Numbers, OutputContext, grounding, verify_claims

CHECKS = ["GR-PRODUCT", "GR-PLACEHOLDER", "GR-NUMBER", "GR-HANDLE", "GR-CITATION"]


def failures(
    text: str, context: OutputContext | None = None, nums: Numbers | None = None
) -> dict[str, tuple[str, ...]]:
    verdicts = grounding(text, context or ctx(), pack(), nums)
    assert [v.rule_id for v in verdicts] == CHECKS  # one verdict per check, always, in order
    return {v.rule_id: v.detail for v in verdicts if v.action == "fail"}


# --- numbers ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Death by suicide within twelve months is excluded [E1].",  # a number word, verbatim
        "Of the premiums paid, 80% is refunded [E1].",
        "It covers you to age 60 [R2].",  # an engine value
        "प्रीमियम का ८०% लौटाया जाता है [E1]।",  # Devanagari digits
        "The section 10(10D) exemption has conditions [E3].",
        "It pays {{cover:999N001V02}} for {{term:999N001V02}} years [R1].",  # placeholders, a UIN
    ],
)
def test_a_number_from_the_engine_or_cited_evidence_passes(text: str) -> None:
    assert "GR-NUMBER" not in failures(text, nums=numbers())


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("Death by suicide within 24 months is excluded [E1].", "24"),
        ("It covers you to age 60 [E1].", "60"),  # in the engine, but not in what it cites
        ("There are fifteen exclusions [E1].", "fifteen"),
        ("Of the premiums paid, 8% is refunded [E1].", "8"),  # 80% is not 8%
        ("The deduction limit is ₹2 lakh [E3].", "2"),
    ],
)
def test_a_number_not_in_the_engine_or_cited_evidence_fails(text: str, token: str) -> None:
    detail = failures(text)["GR-NUMBER"]

    assert detail == (
        f'GR-NUMBER sentence 1: "{token}" is not in ENGINE_RESULT or in the evidence this'
        " sentence cites",
    )


# --- placeholders -------------------------------------------------------------------------------


def test_a_placeholder_for_a_ranked_option_passes() -> None:
    assert failures("It pays {{cover:999N001V02}} [R1].", nums=numbers()) == {}


@pytest.mark.parametrize(
    ("text", "nums", "reason"),
    [
        ("It pays {{cover:999N002V01}} [R1].", numbers(), "NOT_RANKED"),
        (
            "It costs {{premium:999N001V02}} [R1].",
            Numbers(ranking(option(1, TERM, priced=False)), suitability()),
            "VALUE_MISSING",
        ),
        ("It saves {{discount}} [R1].", numbers(), "UNKNOWN_PLACEHOLDER"),
        ("It pays {{cover:999N001V02} [R1].", numbers(), "MALFORMED"),
        ("It pays {{cover:999N001V02}} [R1].", None, "write no placeholders here"),
    ],
)
def test_a_placeholder_error_is_a_grounding_failure(
    text: str, nums: Numbers | None, reason: str
) -> None:
    assert failures(text, nums=nums)["GR-PLACEHOLDER"] == (f"GR-PLACEHOLDER sentence 1: {reason}",)


# --- products (I3) ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Suraksha Term Shield could suit you.",
        "Plan 999N001V02 could suit you.",
        "suraksha term\u200b shield could suit you.",  # case and a zero-width character
    ],
)
def test_before_s3_no_product_unless_the_customer_named_it(text: str) -> None:
    before_s3 = ctx(fsm_state="S2", route=Route.GEN_CONVERSE)

    assert failures(text, before_s3)["GR-PRODUCT"] == (
        "GR-PRODUCT sentence 1: it names a product that is not in ENGINE_RESULT",
    )
    assert "GR-PRODUCT" not in failures(text, ctx(fsm_state="S2", customer_uins=frozenset({TERM})))


def test_before_s3_an_engine_fact_does_not_allow_a_product() -> None:
    assert "GR-PRODUCT" in failures("Suraksha Term Shield fits [R1].", ctx(fsm_state="S2"))


def test_in_s3_only_products_in_engine_result() -> None:
    assert "GR-PRODUCT" not in failures("Suraksha Term Shield fits your need [R1].")
    # The longer name is a different, unranked product, not a mention of the ranked one.
    assert "GR-PRODUCT" in failures("Suraksha Term Shield ROP also fits [R1].")
    assert "GR-PRODUCT" in failures(f"Plan {ROP} also fits [R1].")
    assert "GR-PRODUCT" in failures("The rider 999A009V01 adds cover [R1].")
    assert "GR-PRODUCT" not in failures("A critical illness rider is a benefit type [E1].")


# --- handles --------------------------------------------------------------------------------------


def test_a_handle_not_issued_this_turn_fails() -> None:
    assert failures("Death by suicide is excluded [E1, E9].")["GR-HANDLE"] == (
        "GR-HANDLE: [E9] was not issued this turn",
    )


def test_a_handle_trimmed_out_of_the_envelope_is_foreign() -> None:
    every = handles()
    seen = TurnHandles({"E1": every.evidence["E1"]}, every.engine)  # Envelope.handles

    assert "GR-HANDLE" in failures("It pays guaranteed additions [E2].", ctx(handles=seen))


def test_a_handle_broken_by_a_zero_width_character_fails() -> None:
    assert failures("Death by suicide is excluded [E\u200b1].")["GR-HANDLE"] == (
        "GR-HANDLE: a citation is malformed; write handles exactly as [E1] or [R1]",
    )


# --- factual sentences --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The waiting period applies to illness.",
        "प्रतीक्षा अवधि लागू होती है।",  # Hindi term
        "The premium stays level.",
        "It pays {{cover:999N001V02}}.",  # a placeholder is a number
        "The free-look period lets you return it [E9].",  # only a foreign handle
    ],
)
def test_a_factual_sentence_without_an_issued_handle_fails(text: str) -> None:
    assert failures(text, nums=numbers())["GR-CITATION"] == (
        "GR-CITATION sentence 1: it states a product, regulatory or tax fact or a number without"
        " a handle",
    )


@pytest.mark.parametrize(
    "text",
    [
        "Thanks, that helps me understand what you are looking for.",
        "Please read its disclosures before you decide.",
        "Death by suicide within twelve months is excluded. [E1]",  # the handle follows the stop
        "Section 45 limits when a policy can be questioned [E4].",
    ],
)
def test_small_talk_and_cited_facts_pass(text: str) -> None:
    assert failures(text) == {}


def test_the_error_list_numbers_sentences() -> None:
    detail = failures("It fits [R1]. The waiting period applies.")["GR-CITATION"]

    assert detail[0].startswith("GR-CITATION sentence 2:")


# --- claim-level NLI ----------------------------------------------------------------------------


async def nli(text: str, models: Models, **update: Any) -> tuple[str, float | None]:
    context = ctx(**update.pop("context", {}))
    async with gateway(models) as gw:
        verdict = await verify_claims(gw, text, context, settings(**update))
    assert (verdict.rail, verdict.rule_id) == ("grounding", "GR-NLI")
    return verdict.action, verdict.score


@pytest.mark.asyncio
async def test_every_cited_sentence_is_checked_on_gen_recommend() -> None:
    models = Models()
    text = "Suicide is excluded [E1]. It fits [R1]. Please read the disclosures."

    assert await nli(text, models) == ("pass", 1.0)
    assert models.calls("verify-claims") == 2


@pytest.mark.asyncio
async def test_a_claim_its_evidence_does_not_entail_fails() -> None:
    models = Models()

    assert await nli("Suicide is excluded [E1]. UNSUPPORTED claim [E1].", models) == ("fail", 0.5)


@pytest.mark.asyncio
async def test_the_claim_is_checked_against_what_it_cites_with_placeholders_named() -> None:
    models = Models()
    await nli("It pays {{cover:999N001V02}} [R1, E1].", models)

    [(route, messages)] = models.requests
    content = messages[0]["content"]
    assert route == "verify-claims"
    assert content.startswith("<evidence>RANK-01 Ranked option 1 {")
    assert content.endswith("<claim>It pays [cover] .</claim>")  # handles out, placeholder named
    assert '"cover_to_age"' not in content  # R2 is not cited
    assert "RANK-01" in content and "Death by suicide" in content


@pytest.mark.asyncio
async def test_gen_converse_samples_by_turn_and_the_draw_is_repeatable() -> None:
    converse = {"route": Route.GEN_CONVERSE, "fsm_state": "S2"}
    text = "Suicide is excluded [E1]."

    assert await nli(text, Models(), context=converse, verify_sample_rate=0) == (
        "not_sampled",
        None,
    )
    assert await nli(text, Models(), context=converse, verify_sample_rate=1) == ("pass", 1.0)
    for _ in range(5):
        turn = {**converse, "turn_id": uuid4()}
        first = await nli(text, Models(), context=turn, verify_sample_rate=0.5)
        assert await nli(text, Models(), context=turn, verify_sample_rate=0.5) == first


@pytest.mark.asyncio
async def test_nothing_cited_means_nothing_to_verify() -> None:
    models = Models()

    assert await nli("Thanks for waiting.", models) == ("pass", None)
    assert models.requests == []


@pytest.mark.asyncio
async def test_a_down_verifier_is_unavailable_not_a_pass() -> None:
    assert await nli("Suicide is excluded [E1].", Models(verify_down=True)) == ("unavailable", None)


def test_engine_facts_with_a_uin_allow_that_product_in_s3() -> None:
    facts = [EngineFact("RANK-02", "Ranked option 2", {"uin": ROP})]
    context = ctx(handles=issue([], facts))

    assert "GR-PRODUCT" not in failures("Suraksha Term Shield ROP fits [R1].", context)
