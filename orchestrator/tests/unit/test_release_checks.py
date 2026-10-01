"""Rail 8 (TDD §4.2, §3.8; I4): disclosure-set hashes, guard-input in output mode, the
cross-session leak check and the DUMMY rail."""

import hashlib
from dataclasses import replace
from uuid import uuid4

import pytest
from output_support import (
    SESSION,
    SUBJECT,
    TERM,
    Models,
    ctx,
    gateway,
    numbers,
    render_s3,
    seed_set,
)

from surakshasetu.compose.composer import Rendered
from surakshasetu.domain.models import DisclosureItem, DisclosureSet
from surakshasetu.rails.output import Verdict, _set_sha256, disclosures, dummy, guard, leaks

# domain-services' DisclosureRegistryTest pins the same seed set: both tiers must agree.
SEED_SET_SHA256 = "fdcc70b9a658fda8ce69daf5fec4c0420de4a784911541b292a0e53e5450eee8"
NARRATIVE = "Suraksha Term Shield fits the need you described [R1]."


def s3(sets: dict[str, DisclosureSet] | None = None) -> Rendered:
    return render_s3(ctx(), sets)(NARRATIVE)


def retext(rendered: Rendered, old: str, new: str) -> Rendered:
    """The rendered turn with `old` replaced everywhere and the hash recomputed."""
    text = rendered.text.replace(old, new)
    return replace(
        rendered,
        text=text,
        parts=[(pid, part.replace(old, new)) for pid, part in rendered.parts],
        rendered_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def check(rendered: Rendered, sets: dict[str, DisclosureSet] | None = None) -> Verdict:
    return disclosures(rendered, numbers(), {TERM: seed_set()} if sets is None else sets)


# --- disclosures (I4) ---------------------------------------------------------------------------


def test_the_python_set_hash_equals_the_java_pinned_seed_hash() -> None:
    shown = seed_set(TERM, "en-IN", "web")

    assert shown.set_sha256 == SEED_SET_SHA256
    assert _set_sha256(shown) == SEED_SET_SHA256


def test_a_composed_turn_with_the_registry_set_passes() -> None:
    assert check(s3()) == Verdict("release", "RC-DISCLOSURE", "pass")


def test_an_edited_body_in_the_released_text_blocks() -> None:
    first = seed_set().items[0].body

    verdict = check(retext(s3(), first, first.replace("Approved", "Approvd")))

    assert verdict.action == "block"
    assert verdict.detail == (f"{TERM}: disclosure bodies differ from the registry",)


def test_a_body_that_no_longer_matches_its_registry_hash_blocks() -> None:
    registry = seed_set()
    edited = DisclosureItem(
        disclosure_id=registry.items[0].disclosure_id,
        body=registry.items[0].body + " (edited)",
        body_sha256=registry.items[0].body_sha256,
    )
    tampered = registry.model_copy(update={"items": [edited, *registry.items[1:]]})

    verdict = check(s3({TERM: tampered}), {TERM: tampered})

    assert verdict.action == "block"
    assert f"{TERM}: a body hash differs from the registry" in verdict.detail
    assert f"{TERM}: set_sha256 differs from the registry" in verdict.detail


def test_a_dropped_item_blocks() -> None:
    registry = seed_set()
    short = registry.model_copy(update={"items": registry.items[:-1]})

    verdict = check(s3({TERM: short}), {TERM: short})

    assert verdict.detail == (f"{TERM}: set_sha256 differs from the registry",)


def test_a_rendered_set_hash_other_than_the_registry_blocks() -> None:
    rendered = replace(s3(), disclosure_hashes={TERM: "0" * 64})

    assert check(rendered).detail == (f"{TERM}: set_sha256 differs from the registry",)


def test_a_missing_disclosure_part_blocks() -> None:
    rendered = s3()
    without = replace(rendered, parts=[p for p in rendered.parts if p[0] != f"disclosures:{TERM}"])

    assert check(without).detail == (f"{TERM}: disclosure set missing",)
    assert check(rendered, {}).detail == (f"{TERM}: disclosure set missing",)


def test_text_changed_after_composition_blocks() -> None:
    rendered = replace(s3(), text=s3().text + "\nP.S.")

    assert "rendered_sha256 does not match the text" in check(rendered).detail


def test_a_turn_without_ranked_options_has_no_set_to_check() -> None:
    plain = "Thanks."
    rendered = Rendered(plain, hashlib.sha256(plain.encode()).hexdigest(), [], {}, [], {}, {})

    assert disclosures(rendered, None, {}).action == "pass"


# --- the leak check -----------------------------------------------------------------------------


def test_a_composed_s3_turn_is_not_a_leak() -> None:
    assert leaks(s3().text, ctx(), {TERM: seed_set()}) == Verdict("release", "RC-LEAK", "pass")


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Your number <IN_MOBILE_1> is noted.", "redaction_token"),
        ("Your email &lt;EMAIL_ADDRESS_2&gt; is noted.", "redaction_token"),
        (f"Reference {uuid4()}.", "foreign_id"),
        ("Call 9876543210 for help.", "pii:IN_MOBILE"),
        ("Write to someone.else@example.com.", "pii:EMAIL_ADDRESS"),
    ],
)
def test_a_cross_session_leak_blocks(text: str, kind: str) -> None:
    verdict = leaks(text, ctx(), {})

    assert verdict.action == "block"
    assert verdict.detail == (kind,)  # the kind, never the value


def test_this_sessions_own_ids_and_values_are_not_leaks() -> None:
    own = ctx(own_pii=frozenset({"9876543210"}))

    assert leaks(f"Session {SESSION}, subject {SUBJECT}.", own, {}).action == "pass"
    assert leaks("We will call 9876543210.", own, {}).action == "pass"


def test_approved_registry_text_is_not_a_leak() -> None:
    registry = seed_set()
    helpline = DisclosureItem(
        disclosure_id="DISC-GRIEVANCE-99",
        body="Grievances: grievance@insurer.example.",
        body_sha256="0" * 64,
    )
    shown = registry.model_copy(update={"items": [*registry.items, helpline]})

    assert leaks("Grievances: grievance@insurer.example.", ctx(), {TERM: shown}).action == "pass"


# --- DUMMY --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("env", "action"), [("pilot", "block"), ("prod", "block")])
def test_dummy_text_blocks_in_pilot_and_prod(env: str, action: str) -> None:
    assert dummy("DUMMY: a. DUMMY: b.", env) == Verdict("release", "RC-DUMMY", action, 2.0)


@pytest.mark.parametrize("env", ["dev", "test"])
def test_dummy_text_is_counted_in_dev_and_test(env: str) -> None:
    assert dummy("DUMMY: a.", env) == Verdict("release", "RC-DUMMY", "count", 1.0)


def test_no_dummy_text_passes() -> None:
    assert dummy("Approved text.", "prod") == Verdict("release", "RC-DUMMY", "pass", 0.0)


# --- guard-input in output mode -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_guard_reads_the_reply_in_output_mode() -> None:
    models = Models()
    async with gateway(models) as gw:
        verdict = await guard(gw, "It fits [R1].", ctx())

    assert verdict == Verdict("release", "RC-GUARD", "pass")
    [(route, messages)] = models.requests
    assert route == "guard-input"
    assert messages == [
        {"role": "user", "content": "Which plan suits me?"},
        {"role": "assistant", "content": "It fits [R1]."},
    ]


@pytest.mark.asyncio
async def test_an_unsafe_reply_blocks() -> None:
    async with gateway(Models(safety="unsafe S11")) as gw:
        verdict = await guard(gw, "It fits [R1].", ctx())

    assert verdict == Verdict("release", "RC-GUARD", "block", detail=("guard: unsafe S11",))


@pytest.mark.asyncio
async def test_a_down_guard_blocks_the_release() -> None:
    async with gateway(Models(guard_down=True)) as gw:
        verdict = await guard(gw, "It fits [R1].", ctx())

    assert verdict == Verdict("release", "RC-GUARD", "block", detail=("guard-input unavailable",))


@pytest.mark.asyncio
async def test_without_model_text_there_is_nothing_to_classify() -> None:
    models = Models(guard_down=True)
    async with gateway(models) as gw:
        verdict = await guard(gw, None, ctx())

    assert verdict.action == "pass"
    assert models.requests == []
