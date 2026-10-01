"""The mandatory and contextual filters, checked by what they let through in an in-process Qdrant
(real filter semantics, no network), and by their shape where the shape is the contract."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from qdrant_client import models
from retrieval_support import PINS, FakeTei, ctx, payload, service

from surakshasetu.retrieval.service import search_filter

CLAUSE = "DUMMY: The free look period is thirty days."


def _keys(flt: models.Filter) -> list[str]:
    assert isinstance(flt.must, list)
    return [c.key for c in flt.must if isinstance(c, models.FieldCondition)]


def test_the_mandatory_filters_are_always_there() -> None:
    for domain in ("regulatory", "product", "tax"):
        flt = search_filter(domain, PINS[domain], ctx())  # type: ignore[arg-type]

        assert _keys(flt) == ["snapshot_id", "status", "review.status", "effective_from"]
        assert isinstance(flt.must, list)
        effective_to = flt.must[-1]
        assert isinstance(effective_to, models.Filter) and effective_to.should


def test_as_of_compares_on_the_ist_calendar_date() -> None:
    late_evening_utc = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)  # already 30 Sep in India
    flt = search_filter("regulatory", PINS["regulatory"], ctx(as_of=late_evening_utc))

    assert isinstance(flt.must, list)
    effective_from = flt.must[3]
    assert isinstance(effective_from, models.FieldCondition) and effective_from.range
    assert effective_from.range.lte == datetime(2026, 9, 30, tzinfo=UTC)  # type: ignore[union-attr]


def test_contextual_filters_apply_to_their_own_collection_only() -> None:
    context = ctx(focus_uins=["999N001V02"], regime="old", tax_year="2025-26")

    assert _keys(search_filter("product", PINS["product"], context))[-1] == "product_uin"
    assert _keys(search_filter("tax", PINS["tax"], context))[-2:] == ["regime", "tax_years"]
    assert "product_uin" not in _keys(search_filter("regulatory", PINS["regulatory"], context))


async def _found(tmp_path: Path, payloads: list, **context: object) -> set[str]:  # type: ignore[type-arg]
    tei = FakeTei({p.text: 0.9 for p in payloads})
    svc = await service(tmp_path, payloads, tei)
    selection = await svc.select("What is the free-look period?", ctx(**context))
    return {s.payload.chunk_id for s in selection.evidence}


@pytest.mark.asyncio
async def test_only_chunks_in_force_on_the_as_of_date_are_found(tmp_path: Path) -> None:
    current = payload("regulatory", "mc", ["IRDAI", "MC", "5. Free look"], CLAUSE)
    future = payload(
        "regulatory",
        "mc-next",
        ["IRDAI", "MC next", "5. Free look"],
        "DUMMY: future.",
        effective_from=date(2026, 10, 1),
    )
    expired = payload(
        "regulatory",
        "mc-old",
        ["IRDAI", "MC old", "5. Free look"],
        "DUMMY: expired.",
        effective_from=date(2025, 4, 1),
        effective_to=date(2026, 9, 28),
    )
    last_day = payload(
        "regulatory",
        "mc-ending",
        ["IRDAI", "MC ending", "5. Free look"],
        "DUMMY: ends today.",
        effective_from=date(2025, 4, 1),
        effective_to=date(2026, 9, 29),
    )

    found = await _found(tmp_path, [current, future, expired, last_day])

    assert found == {current.chunk_id, last_day.chunk_id}


@pytest.mark.asyncio
async def test_only_the_pinned_snapshot_is_searched(tmp_path: Path) -> None:
    pinned = payload("regulatory", "mc", ["IRDAI", "MC", "5. Free look"], CLAUSE)
    newer = payload(
        "regulatory", "mc", ["IRDAI", "MC", "5. Free look"], CLAUSE, snapshot_id="regulatory-next"
    )

    found = await _found(tmp_path, [pinned, newer])

    assert found == {pinned.chunk_id}  # same chunk_id, but only the pinned snapshot's point


@pytest.mark.asyncio
async def test_a_focus_uin_narrows_the_product_collection(tmp_path: Path) -> None:
    ours = payload(
        "product", "999N001V02:pw-v2", ["Term (999N001V02)", "PW v2", "4. Free look"], CLAUSE
    )
    other = payload(
        "product",
        "999N002V01:pw-v2",
        ["ROP (999N002V01)", "PW v2", "4. Free look"],
        "DUMMY: ROP free look.",
        product_uin="999N002V01",
    )

    found = await _found(tmp_path, [ours, other], focus_uins=["999N001V02"])

    assert found == {ours.chunk_id}


@pytest.mark.asyncio
async def test_regime_and_tax_year_narrow_the_tax_collection(tmp_path: Path) -> None:
    old = payload(
        "tax", "s123", ["ITA 2025", "Section 123", "123(1) Deduction"], "DUMMY: old.", regime="old"
    )
    new = payload(
        "tax", "s202", ["ITA 2025", "Section 202", "202(2) New regime"], "DUMMY: new.", regime="new"
    )
    both_1961 = payload(
        "tax",
        "s80c",
        ["ITA 1961", "Section 80C", "80C(1) Deduction"],
        "DUMMY: 1961.",
        tax_years=["2025-26"],
    )
    both_2025 = payload("tax", "faq", ["CBDT", "FAQ", "2. Section 80C"], "DUMMY: faq.")

    found = await _found(
        tmp_path,
        [old, new, both_1961, both_2025],
        regime="old",
        tax_year="2026-27",
        entities=["tax"],
    )

    assert found == {old.chunk_id, both_2025.chunk_id}


def test_as_of_must_carry_a_timezone() -> None:
    with pytest.raises(ValueError, match="timezone"):
        ctx(as_of=datetime(2026, 9, 29, 6, 0))
