"""Money in S2 (Step 20, TDD §3.7): every amount is understood deterministically and read back,
annual flows annualised ("₹9,60,000 a year"), lump sums as an amount; "80k" with no period asks
a month or a year and is never assumed; "none" is zero and needs no read-back."""

from typing import Any

import pytest
from test_runtime_nodes import run
from test_s0 import ids
from test_s2 import audited, s2_turn, stored  # noqa: F401  (the autouse fixtures)

from surakshasetu.graph.states.s1 import Answer
from surakshasetu.graph.states.s2 import Period, _money


@pytest.mark.parametrize(
    ("said", "flow", "value"),
    [
        ("12 LPA", True, "1200000"),
        ("80k a month", True, "960000"),
        ("80k monthly", True, "960000"),
        ("1 lakh a month", True, "1200000"),
        ("80,000 per month", True, "960000"),
        ("24 lakh", True, "2400000"),
        ("₹24,00,000", True, "2400000"),
        ("2400000", True, "2400000"),
        ("८०k a month", True, "960000"),  # Devanagari digits
        ("80k a year", True, "80000"),
        ("35 lakh", False, "3500000"),
        ("1.5 crore", False, "15000000"),
        ("80k", False, "80000"),  # a lump sum has no period
    ],
)
def test_amounts_are_understood_and_annualised(said: str, flow: bool, value: str) -> None:
    assert _money(said, flow=flow) == Answer(value, derived=True)


@pytest.mark.parametrize("said", ["none", "no", "nil", "0", "कोई नहीं"])
def test_none_is_zero_and_needs_no_read_back(said: str) -> None:
    assert _money(said, flow=True) == Answer("0")


def test_a_declined_amount_is_declined() -> None:
    assert _money("I'd prefer not to say", flow=True) == Answer(None, declined=True)


def test_80k_with_no_period_asks_the_period() -> None:
    assert _money("80k", flow=True) == Period(80000)


def test_an_unusable_answer_is_not_a_value() -> None:
    assert _money("a fair amount, I think, depending on the bonus this year", flow=True) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said", "text"),
    [
        ("12 LPA", "Just to check: ₹12,00,000 a year. Is that right?"),
        ("80k a month", "Just to check: ₹9,60,000 a year. Is that right?"),
    ],
)
async def test_an_income_is_read_back_annualised(said: str, text: str) -> None:
    t = s2_turn(said, prompt="s2.ask:annual_income_inr")
    await run(t)
    assert t.released is not None and t.released.text == text
    assert t.next is not None and t.next.last_prompt_id == "s2.confirm:annual_income_inr"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "text", "value"),
    [
        ({"type": "PERIOD", "payload": {"period": "month"}}, None, "960000"),
        (None, "a month", "960000"),
        (None, "per year", "80000"),
    ],
)
async def test_80k_asks_month_or_year_then_reads_back_the_annual_amount(
    action: dict[str, Any] | None, text: str | None, value: str
) -> None:
    asked = s2_turn("80k", prompt="s2.ask:annual_income_inr")
    await run(asked)
    assert ids(asked) == ["template:period_ask"]
    assert asked.released is not None
    assert asked.released.text == "Is ₹80,000 a month or a year?"
    assert asked.slot_rows == []  # never assumed
    assert asked.next is not None and asked.next.last_prompt_id is not None

    t = s2_turn(text, action=action, prompt=asked.next.last_prompt_id)
    await run(t)

    assert [(r.slot, r.value, r.status) for r in t.slot_rows] == [
        ("annual_income_inr", value, "proposed")
    ]
    amount = "₹9,60,000" if value == "960000" else "₹80,000"
    assert t.released is not None
    assert t.released.text == f"Just to check: {amount} a year. Is that right?"


@pytest.mark.asyncio
async def test_a_period_answer_that_says_neither_asks_again() -> None:
    t = s2_turn("not sure", prompt="s2.period:annual_income_inr:80000")
    await run(t)
    assert ids(t) == ["template:period_ask"] and t.slot_rows == []


@pytest.mark.asyncio
async def test_an_income_from_nlu_extract_keeps_the_period_its_evidence_states() -> None:
    from runtime_support import Models
    from test_s1 import candidate

    models = Models(slots=(candidate("annual_income_inr", 80000, "80k a month"),))
    t = s2_turn("about 80k a month", models=models, prompt="s2.ask:annual_income_inr")
    await run(t)
    assert [(r.slot, r.value) for r in t.slot_rows] == [("annual_income_inr", "960000")]
