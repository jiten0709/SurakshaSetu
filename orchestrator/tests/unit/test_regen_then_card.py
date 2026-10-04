"""The output policy (TDD §4.2, §3.8, §1.5): rails 6-7 on the draft, one regeneration with the
error list, the deterministic card after a second failure or when verify-claims is down; rail 8 on
what was rendered, and a template plus an advisor offer when a release check blocks. Every verdict
is one GUARD_VERDICT event with its rule id, in the fixed order."""

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest
from compose_support import bundle
from output_support import TERM, Models, Recorder, ctx, gateway, numbers, pack, render_s3, seed_set
from output_support import settings as make_settings

from surakshasetu.audit.events import EventType
from surakshasetu.compose.composer import Rendered
from surakshasetu.config import Settings
from surakshasetu.gateway import Route
from surakshasetu.logging import configure_logging
from surakshasetu.rails.output import OutputContext, Released, release

CLEAN = "Suraksha Term Shield fits the need you described [R1]. Suicide is excluded [E1]."
SUPERLATIVE = "Suraksha Term Shield is the best plan for you [R1]."
UNSUPPORTED = "Suraksha Term Shield fits [R1]. UNSUPPORTED: it also pays on lapse [E1]."
GROUNDING = ["GR-PRODUCT", "GR-PLACEHOLDER", "GR-NUMBER", "GR-HANDLE", "GR-CITATION", "GR-NLI"]
RELEASE = ["RC-DISCLOSURE", "RC-GUARD", "RC-LEAK", "RC-DUMMY"]


class Renders:
    """The S3 composer, recording what the policy handed it."""

    def __init__(self, context: OutputContext, sets: Any = None) -> None:
        self.inner = render_s3(context, sets)
        self.seen: list[str | None] = []

    def __call__(self, narrative: str | None) -> Rendered:
        self.seen.append(narrative)
        return self.inner(narrative)


class Regenerations:
    """The caller's regeneration: a new envelope with the corrections, then gen-recommend."""

    def __init__(self, *texts: str | None) -> None:
        self.texts = list(texts)
        self.errors: list[list[str]] = []

    async def __call__(self, errors: list[str]) -> str | None:
        self.errors.append(errors)
        return self.texts.pop(0)


async def run(
    monkeypatch: pytest.MonkeyPatch,
    draft: str | None,
    *regenerated: str | None,
    models: Models | None = None,
    context: OutputContext | None = None,
    sets: Any = None,
    settings: Settings | None = None,
) -> tuple[Released, Recorder, Renders, Regenerations]:
    recorder = Recorder(monkeypatch)
    context = context or ctx()
    renders, regenerate = Renders(context, sets), Regenerations(*regenerated)
    async with gateway(models or Models()) as gw:
        released = await release(
            cast(Any, "conn"),
            cast(Any, "keys"),
            gw,
            context,
            pack=pack(),
            settings=settings or make_settings(),
            bundle=bundle(),
            draft=draft,
            regenerate=regenerate,
            render=renders,
            numbers=numbers(),
            disclosure_sets={TERM: seed_set()} if sets is None else sets,
        )
    return released, recorder, renders, regenerate


def attempt(lexicon: str = "none", **actions: str) -> list[tuple[str, str]]:
    """One attempt's rule ids and actions: everything passes unless named."""
    lex = [(lexicon, actions.pop(lexicon.replace("-", "_"), "pass"))]
    return lex + [(rule, actions.pop(rule.replace("-", "_"), "pass")) for rule in GROUNDING]


def release_checks(**actions: str) -> list[tuple[str, str]]:
    return [(rule, actions.pop(rule.replace("-", "_"), "pass")) for rule in RELEASE]


def rules(recorder: Recorder) -> list[tuple[str, str]]:
    return [(rule, action) for _, rule, action in recorder.rules()]


@pytest.mark.asyncio
async def test_a_clean_draft_is_released_as_written(monkeypatch: pytest.MonkeyPatch) -> None:
    released, recorder, renders, regenerate = await run(monkeypatch, CLEAN)

    assert released.kind == "narrative"
    assert renders.seen == [CLEAN]  # byte for byte: the rails never edit model text
    assert regenerate.errors == []
    assert released.rendered is not None and released.text == released.rendered.text
    assert "[Source: Ranked option 1]" in released.text
    assert rules(recorder) == attempt() + release_checks(RC_DUMMY="count")


@pytest.mark.asyncio
async def test_every_verdict_is_one_guard_verdict_event(monkeypatch: pytest.MonkeyPatch) -> None:
    released, recorder, _, _ = await run(monkeypatch, CLEAN)

    assert len(recorder.events) == 11 == len(released.verdicts)
    for event in recorder.events:
        assert event["event_type"] is EventType.GUARD_VERDICT
        assert event["session_id"] == ctx().session_id and event["key_ref"] == "key-ref"
        assert event["pins"] == {"prompt_bundle": "pb-2026.10.3"}
        header = event["header"]
        assert header.pack_version == ("2026.09.1" if header.rail == "lexicon" else None)
    assert released.verdicts["grounding:GR-NLI"] == "pass"
    assert released.verdicts["release:RC-DUMMY"] == "count"


@pytest.mark.asyncio
async def test_a_failed_draft_is_regenerated_once_with_the_error_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released, recorder, renders, regenerate = await run(monkeypatch, SUPERLATIVE, CLEAN)

    assert released.kind == "regenerated"
    assert regenerate.errors == [['LX-SUP-02 sentence 1: "best plan" is not allowed']]
    assert renders.seen == [CLEAN]
    assert rules(recorder) == (
        attempt("LX-SUP-02", LX_SUP_02="regenerate", GR_NLI="skipped")
        + attempt()
        + release_checks(RC_DUMMY="count")
    )


@pytest.mark.asyncio
async def test_a_second_failure_falls_back_to_the_deterministic_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released, recorder, renders, regenerate = await run(monkeypatch, SUPERLATIVE, SUPERLATIVE)

    assert released.kind == "fallback"
    assert len(regenerate.errors) == 1
    assert renders.seen == [None]  # the card: templates, engine values, verbatim disclosures
    card = bundle().templates["en-IN"].recommendation.deterministic_card
    assert card in released.text and "best plan" not in released.text
    assert rules(recorder) == (
        attempt("LX-SUP-02", LX_SUP_02="regenerate", GR_NLI="skipped")
        + attempt("LX-SUP-02", LX_SUP_02="fallback", GR_NLI="skipped")
        + release_checks(RC_DUMMY="count")
    )


@pytest.mark.asyncio
async def test_an_unsupported_claim_is_regenerated(monkeypatch: pytest.MonkeyPatch) -> None:
    released, recorder, _, regenerate = await run(monkeypatch, UNSUPPORTED, CLEAN)

    assert released.kind == "regenerated"
    assert regenerate.errors == [["GR-NLI sentence 2: it is not supported by what it cites"]]
    nli = [e["header"] for e in recorder.events if e["header"].rule_id == "GR-NLI"]
    assert [(h.action, h.score) for h in nli] == [("regenerate", 0.5), ("pass", 1.0)]


@pytest.mark.asyncio
async def test_a_down_regeneration_route_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    released, recorder, renders, _ = await run(monkeypatch, SUPERLATIVE, None)

    assert released.kind == "fallback"
    assert renders.seen == [None]
    assert rules(recorder) == attempt(
        "LX-SUP-02", LX_SUP_02="regenerate", GR_NLI="skipped"
    ) + release_checks(RC_DUMMY="count")


@pytest.mark.asyncio
async def test_a_down_verifier_in_s3_holds_the_narrative(monkeypatch: pytest.MonkeyPatch) -> None:
    released, recorder, renders, regenerate = await run(
        monkeypatch, CLEAN, models=Models(verify_down=True)
    )

    assert released.kind == "fallback"
    assert regenerate.errors == []  # an outage is not fixed by rewording: no regeneration
    assert renders.seen == [None]
    assert rules(recorder) == attempt(GR_NLI="fallback") + release_checks(RC_DUMMY="count")


@pytest.mark.asyncio
async def test_a_down_generation_route_goes_straight_to_the_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    released, recorder, renders, _ = await run(monkeypatch, None)

    assert released.kind == "fallback"
    assert renders.seen == [None]
    assert rules(recorder) == release_checks(RC_DUMMY="count")


@pytest.mark.asyncio
async def test_an_unsafe_reply_blocks_the_release(monkeypatch: pytest.MonkeyPatch) -> None:
    released, recorder, _, _ = await run(monkeypatch, CLEAN, models=Models(safety="unsafe S1"))
    scripts = bundle().templates["en-IN"].scripts

    assert released.kind == "blocked" and released.rendered is None
    assert released.text == f"{scripts.release_blocked}\n{scripts.advisor_offer}"
    assert released.verdicts["release:RC-GUARD"] == "block"
    assert rules(recorder)[-4:] == release_checks(RC_GUARD="block", RC_DUMMY="count")


@pytest.mark.asyncio
async def test_a_down_guard_blocks_even_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    released, _, _, _ = await run(monkeypatch, CLEAN, models=Models(guard_down=True))

    assert released.kind == "blocked"


@pytest.mark.asyncio
async def test_a_disclosure_mismatch_blocks_the_release(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = seed_set()
    stale = registry.model_copy(update={"set_sha256": "0" * 64})

    released, recorder, _, _ = await run(monkeypatch, CLEAN, sets={TERM: stale})

    assert released.kind == "blocked"
    assert rules(recorder)[-4:] == release_checks(RC_DISCLOSURE="block", RC_DUMMY="count")


@pytest.mark.asyncio
async def test_dummy_text_blocks_the_release_in_pilot(monkeypatch: pytest.MonkeyPatch) -> None:
    pilot = make_settings().model_copy(update={"env": "pilot"})  # only the env matters here

    released, recorder, _, _ = await run(monkeypatch, CLEAN, settings=pilot)

    assert released.kind == "blocked"
    assert rules(recorder)[-1] == ("RC-DUMMY", "block")


@pytest.mark.asyncio
async def test_s1_s2_samples_nli_and_falls_back_to_the_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = "What is your age?"
    converse = ctx(fsm_state="S2", route=Route.GEN_CONVERSE)

    def render(sentence: str | None) -> Rendered:
        text = f"{sentence} {template}" if sentence else template
        return Rendered(text, hashlib.sha256(text.encode()).hexdigest(), [], {}, [], {}, {})

    recorder = Recorder(monkeypatch)
    async with gateway(Models()) as gw:
        released = await release(
            cast(Any, "conn"),
            cast(Any, "keys"),
            gw,
            converse,
            pack=pack(),
            settings=make_settings(verify_sample_rate=0),
            bundle=bundle(),
            draft="The waiting period is long.",  # a fact without a handle
            regenerate=Regenerations("Thanks, that helps."),
            render=render,
        )

    assert released.kind == "regenerated"
    assert released.text == f"Thanks, that helps. {template}"
    assert rules(recorder)[-8:-4] == [
        ("GR-NUMBER", "pass"),
        ("GR-HANDLE", "pass"),
        ("GR-CITATION", "pass"),
        ("GR-NLI", "not_sampled"),
    ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
async def test_no_model_or_customer_text_reaches_the_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", tmp_path)
    sentinel = "Suraksha Term Shield is the best plan Sentinelson zqxv [R1]."
    context = ctx(customer_text="Zqxvcustomer asks", own_pii=frozenset({"9876543210"}))

    await run(monkeypatch, sentinel, sentinel, context=context)

    logged = capsys.readouterr().out + files(tmp_path)
    assert "output rails attempt 1 failed: LX-SUP-02 -> regenerate" in logged
    assert "output rails attempt 2 failed: LX-SUP-02 -> fallback" in logged
    for secret in ("Sentinelson", "zqxv", "Zqxvcustomer", "best plan", "9876543210"):
        assert secret not in logged


def files(directory: Path) -> str:
    return "".join(p.read_text() for p in directory.iterdir())
