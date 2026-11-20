"""D-6's size limit on the customer turn guard-input's output mode carries (bug #44, approved
2026-11-20). M(c) = UTF-8 bytes + 10 per '@' + 3/5 per ASCII digit bounds the text after the
gateway's PII masking; over context_tokens // 2 - 64, only when SS_GATEWAY_CONTEXT_TOKENS is set,
that call never carries the turn: the release policy drops the draft (RC-GUARD-SIZE fallback) and
compose withholds the lead sentence (skipped). The input rails are unchanged. No network, no
database: models answer through httpx.MockTransport and audit appends are recorded."""

import json
import re
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import httpx
import output_support as out
import pytest
from compose_support import bundle
from hypothesis import example, given
from hypothesis import settings as hypothesis_settings
from hypothesis import strategies as st
from runtime_support import Models, completion, gateway, settings
from test_omniroute_hardening import LIVE_SERVICE
from test_regen_then_card import CLEAN, Regenerations, Renders

from surakshasetu.analysis.nlu import PendingSlotSpec
from surakshasetu.analysis.pipeline import TurnContext, analyse, analyse_turn
from surakshasetu.audit import chain as audit_chain
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.eval import gateway_log
from surakshasetu.gateway import DataClass, RedactionAttestation, Route
from surakshasetu.gateway.adapter import (
    CONTEXT_FLOOR_ROUTES,
    OUTPUT_GUARD_MASKING_IMAGE,
    ascii_digit_count,
    masked_size,
    output_guard_limit,
    output_guard_overlong,
)
from surakshasetu.logging import configure_logging
from surakshasetu.rails.output import Released, release

SID = UUID("0199a1b2-0000-7000-8000-0000000d6d6d")
TID = UUID("0199a1b2-0000-7000-8000-0000000d6d6e")
LIMIT = 4032  # 8192 // 2 - 64
EMAIL = "a" * 23 + "@b.cc"  # 28 bytes, 5 proxy tokens
# Turn texts with no run of 60 letters or digits (the injection rail's base64_run would block
# them) and under the 500-token cap: over the limit by bytes, by the '@' allowance only.
WORDS = "abcdefghij " * 380  # 4,180 bytes, 380 proxy tokens
EMAILS = " ".join([EMAIL] * 99 + ["b" * 58] * 3)  # 3,047 bytes, M = 4,037; redacted M = 2,161


def emails_and_word(word: int) -> str:
    """99 emails and one long word: 496 proxy tokens, under the token cap of 500."""
    return " ".join([EMAIL] * 99) + " " + "a" * word


class CountedModels(Models):
    """runtime_support's models, each answer reporting usage.prompt_tokens: with D-6 set, the
    adapter refuses a reply without it (MALFORMED_RESPONSE). summarise (no call site in the app)
    answers too."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if json.loads(request.content)["model"] == "summarise":
            self.routes.append("summarise")
            response = completion("summarise", "A summary.")
        else:
            response = super().__call__(request)
        body = response.json() | {"usage": {"prompt_tokens": 100, "completion_tokens": 5}}
        return httpx.Response(response.status_code, json=body)


# --- the gateway's PII masking, as OmniRoute v3.8.50 does it -------------------------------------
# src/shared/utils/inputSanitizer.ts:76-105 at v3.8.50, verbatim except JavaScript's \s, which also
# matches Unicode spaces without the u flag (\b and \d are ASCII only there, as re.ASCII is here).
JS_SPACE = "\t\n\v\f\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
PII_PATTERNS = [
    (r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", "[EMAIL_REDACTED]"),
    (r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b", "[CPF_REDACTED]"),
    (r"\b\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}\b", "[CNPJ_REDACTED]"),
    (r"\b(?:\d{4}[-" + JS_SPACE + r"]?){3}\d{4}\b", "[CARD_REDACTED]"),
    (r"\b\(?\d{2}\)?[" + JS_SPACE + r"]?\d{4,5}-?\d{4}\b", "[PHONE_REDACTED]"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "[SSN_REDACTED]"),
]
COMPILED = [(re.compile(p, re.ASCII), r) for p, r in PII_PATTERNS]


def gateway_mask(text: str) -> str:
    """processPII (inputSanitizer.ts:225-240): each pattern is tested on the original text and,
    when it matched, replaced throughout the text masked so far."""
    out = text
    for pattern, replacement in COMPILED:
        if pattern.search(text):
            out = pattern.sub(replacement, out)
    return out


def test_the_masking_bound_is_derived_from_the_patterns_shortest_matches() -> None:
    # (shortest match, replacement): only email, phone and SSN replacements are longer.
    shortest = {
        "[EMAIL_REDACTED]": "a@b.cc",
        "[CPF_REDACTED]": "123.456.789-01",
        "[CNPJ_REDACTED]": "12.345.678/0001-90",
        "[CARD_REDACTED]": "4111111111111111",
        "[PHONE_REDACTED]": "1234567890",
        "[SSN_REDACTED]": "123-45-6789",
    }
    for replacement, match in shortest.items():
        assert gateway_mask(match) == replacement
    growth = {r: len(r) - len(m) for r, m in shortest.items()}
    assert growth["[EMAIL_REDACTED]"] == 10  # one '@' per email: 10 per '@'
    assert growth["[PHONE_REDACTED]"] == 6 and growth["[SSN_REDACTED]"] == 3
    assert max(6 / 10, 3 / 9) == 3 / 5  # per ASCII digit: phone (10 digits) is the worst case
    assert all(growth[r] <= 0 for r in ("[CPF_REDACTED]", "[CNPJ_REDACTED]", "[CARD_REDACTED]"))


@hypothesis_settings(max_examples=3000, deadline=None)
@given(st.text(alphabet="ab.Z_@-()/+%| 0123456789\xa0\u0967", max_size=160))
@example("a@b.cc " * 40)  # email growth is tight: 7 bytes -> 17
@example("1234567890 " * 30)  # phone growth is tight: 11 bytes -> 17
@example("123-45-6789 4111 1111 1111 1111 123.456.789-01")
def test_masked_size_bounds_the_masked_text(text: str) -> None:
    masked = gateway_mask(text)
    assert len(masked.encode("utf-8")) <= masked_size(text)
    assert gateway_mask(masked) == masked  # a second pass changes nothing: the bound holds


def test_only_ascii_digits_are_counted() -> None:
    assert ascii_digit_count("0123456789") == 10
    # Devanagari, Arabic-Indic and fullwidth digits: none is \d to the gateway's patterns
    assert ascii_digit_count("\u0966\u0967\u0968 \u0661\u0662 \uff11\uff12") == 0
    assert masked_size("\u0967" * 10) == 30  # three bytes each, no digit allowance
    assert masked_size("7" * 10) == 10 + 6
    assert masked_size("a@b") == 3 + 10


def test_the_masking_bound_was_read_from_the_verified_live_image() -> None:
    # The tripwire: masked_size encodes v3.8.50's six patterns. If omniroute-live's image moves,
    # bug #43's gate (VERIFIED_IMAGE) fails closed and so does this, until the patterns are read
    # again on the new image and OUTPUT_GUARD_MASKING_IMAGE is updated with them.
    assert gateway_log.VERIFIED_IMAGE == OUTPUT_GUARD_MASKING_IMAGE, (
        "VERIFIED_IMAGE moved: re-read the gateway's PII patterns (inputSanitizer.ts) on the new"
        " image, re-derive masked_size, then update OUTPUT_GUARD_MASKING_IMAGE"
    )
    assert LIVE_SERVICE["image"] == OUTPUT_GUARD_MASKING_IMAGE


# --- the limit and the predicate -----------------------------------------------------------------
@pytest.mark.parametrize(
    ("context_tokens", "limit"), [(8192, 4032), (8193, 4032), (8191, 4031), (16384, 8128)]
)
def test_the_limit_is_half_the_context_less_64(context_tokens: int, limit: int) -> None:
    assert output_guard_limit(context_tokens) == limit
    assert not output_guard_overlong("a" * limit, context_tokens)  # equal passes
    assert output_guard_overlong("a" * (limit + 1), context_tokens)  # one over fails


@pytest.mark.parametrize(
    ("text", "overlong"),
    [
        ("7" * 2520, False),  # M = 2520 + 1512 = 4032
        ("7" * 2521, True),  # M = 2521 + 1512 = 4033: one token by the proxy count
        ("a@b.cc " * 237, False),  # 1,659 bytes, M = 4,029
        ("a@b.cc " * 238, True),  # 1,666 bytes, far under 4,032: the '@' allowance decides
    ],
    ids=["digits-at-limit", "digits-over", "emails-at-237", "emails-at-238"],
)
def test_the_masked_size_not_the_byte_length_decides(text: str, overlong: bool) -> None:
    assert len(text.encode("utf-8")) <= LIMIT
    assert output_guard_overlong(text, 8192) is overlong


def test_unset_never_counts_a_turn_overlong() -> None:
    assert not output_guard_overlong("a@b.cc " * 2000, None)
    assert not output_guard_overlong("7" * 100_000, None)


# --- the input rails never apply the limit: they only produce the string it measures -------------
async def analysed(text: str, context_tokens: int | None = 8192) -> tuple[Any, CountedModels]:
    models = CountedModels()
    config = settings(gateway_context_tokens=context_tokens)
    found = await analyse(
        gateway(models, config),
        text,
        session_id=SID,
        turn_id=TID,
        fsm_state="S1",
        pending=PendingSlotSpec(None),
        settings=config,
    )
    return found, models


OVER = {"digits": "7" * 2521, "emails": emails_and_word(172), "fdfa": "\ufdfa" * 123}


@pytest.mark.asyncio
@pytest.mark.parametrize("context_tokens", [8192, None], ids=["set", "unset"])
@pytest.mark.parametrize("name", list(OVER))
async def test_the_input_rails_never_count_a_turn_overlong_for_the_limit(
    name: str, context_tokens: int | None
) -> None:
    # Route-specific (bug #44): the turn is analysed and acted on as before; only guard-input's
    # output-mode call is kept from carrying it (rails.output.release and compose's lead sentence).
    found, _ = await analysed(OVER[name], context_tokens)

    assert output_guard_overlong(found.result.stored_raw, 8192)
    assert not found.normalised.overlong and not found.result.overlong


@pytest.mark.asyncio
@pytest.mark.parametrize(("word", "over"), [(171, False), (172, True)])
async def test_repeated_emails_under_the_token_cap_are_judged_by_masked_size(
    word: int, over: bool
) -> None:
    found, _ = await analysed(emails_and_word(word))
    stored_raw = found.result.stored_raw

    assert not found.normalised.overlong  # 496 proxy tokens: the token cap doesn't decide
    assert len(stored_raw.encode("utf-8")) < LIMIT  # nor does the byte length
    assert masked_size(stored_raw) == LIMIT + (1 if over else 0)
    assert output_guard_overlong(stored_raw, 8192) is over
    # the emails are tokenised only in the redacted form: measuring it would let the turn through
    assert stored_raw.count("@") == 99 and not output_guard_overlong(found.result.redacted, 8192)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "over"),
    [("\ufdfa" * 122 + "a" * 6, False), ("\ufdfa" * 123, True)],
    ids=["fdfa-at-limit", "fdfa-over"],
)
async def test_the_measured_text_is_after_nfkc_normalisation(text: str, over: bool) -> None:
    # U+FDFA is 3 bytes and normalises to 33: the raw input is far under the limit.
    found, _ = await analysed(text)

    assert len(text.encode("utf-8")) < 400
    assert found.result.stored_raw == found.normalised.text != text
    assert output_guard_overlong(found.result.stored_raw, 8192) is over
    assert not output_guard_overlong(text, 8192)


@pytest.mark.asyncio
async def test_the_measured_text_is_after_redaction() -> None:
    # A card number is masked to [REDACTED] in stored_raw, the text call_guard sends.
    found, _ = await analysed("a" * 4015 + " 4111 1111 1111 1111")

    assert "[REDACTED]" in found.result.stored_raw and "4111" not in found.result.stored_raw
    assert output_guard_overlong(found.normalised.text, 8192)  # the earlier form would be refused
    assert not output_guard_overlong(found.result.stored_raw, 8192)


@pytest.mark.asyncio
async def test_the_token_cap_still_applies_with_or_without_the_setting() -> None:
    for context_tokens in (None, 8192):
        found, _ = await analysed("word " * 600, context_tokens)
        assert found.normalised.overlong and found.result.overlong


@pytest.mark.asyncio
async def test_the_requests_of_the_other_routes_are_unchanged() -> None:
    # guard-input's input mode and nlu-extract still receive the turn's exact stored_raw.
    text = emails_and_word(172)
    on, models_on = await analysed(text, 8192)
    _, models_off = await analysed(text, None)

    for route in ("guard-input", "nlu-extract"):
        assert models_on.messages[route] == models_off.messages[route]
        assert models_on.messages[route][-1]["content"] == on.result.stored_raw
        assert all(m["role"] != "assistant" for m in models_on.messages[route])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "data_class"),
    [
        (Route.NLU_EXTRACT, DataClass.SELF_HOSTED_RAW),
        (Route.VERIFY_CLAIMS, DataClass.SELF_HOSTED_RAW),
        (Route.SUMMARISE, DataClass.SELF_HOSTED_RAW),
        (Route.GEN_RECOMMEND, DataClass.REDACTED),
    ],
)
async def test_the_adapter_applies_no_size_limit_to_any_route(
    route: Route, data_class: DataClass
) -> None:
    # The limit lives in the release policy, for the output guard; the gateway sends any size.
    models = CountedModels()
    messages = [{"role": "user", "content": "7" * 6000}]
    attestation = RedactionAttestation(sha256_hex(messages))
    async with gateway(models, settings(gateway_context_tokens=8192)) as gw:
        result = await gw.call(
            route,
            data_class=data_class,
            messages=messages,
            session_id=SID,
            turn_id=TID,
            fsm_state="S1",
            attestation=attestation if data_class is DataClass.REDACTED else None,
        )
    assert result.tokens_in == 100 and route.value in models.routes
    assert route not in CONTEXT_FLOOR_ROUTES or result.tokens_in < 8192 // 2  # the floor: as before


class FakeConn:
    def execute(self, sql: str, params: Any = None) -> "FakeConn":
        return self


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "context_tokens", "rule", "action"),
    [
        (emails_and_word(172), 8192, "none", "allow"),
        (emails_and_word(172), None, "none", "allow"),
        ("word " * 600, 8192, "overlong", "ask_to_shorten"),
        ("I am 34", 8192, "none", "allow"),
    ],
    ids=["over-limit", "unset", "token-cap", "short"],
)
async def test_the_normalise_verdict_records_the_token_cap_only(
    monkeypatch: pytest.MonkeyPatch, text: str, context_tokens: int | None, rule: str, action: str
) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(audit_chain, "append", lambda conn, keys, **event: events.append(event))
    config = settings(gateway_context_tokens=context_tokens)
    ctx = TurnContext(
        session_id=SID,
        turn_id=TID,
        turn_seq=1,
        turn_key=TID,
        fsm_state="S1",
        pins={},
        channel="web",
        key_ref="key-ref",
        pending=PendingSlotSpec(None),
    )
    await analyse_turn(
        FakeConn(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        gateway(CountedModels(), config),
        text,
        ctx,
        settings=config,
    )

    normalise = [e["header"] for e in events if getattr(e["header"], "rail", None) == "normalise"]
    assert [(h.rule_id, h.action) for h in normalise] == [(rule, action)]


# --- the release policy: guard-input's output mode never carries an over-limit turn --------------
async def released(
    monkeypatch: pytest.MonkeyPatch,
    draft: str | None,
    customer_text: str,
    context_tokens: int | None = 8192,
    *,
    size_limited: bool = False,
) -> tuple[Released, out.Recorder, Renders, Regenerations, out.Models]:
    recorder, models = out.Recorder(monkeypatch), out.Models()
    context = out.ctx(customer_text=customer_text)
    renders, regenerate = Renders(context), Regenerations(CLEAN)
    async with out.gateway(models) as gw:
        result = await release(
            cast(Any, "conn"),
            cast(Any, "keys"),
            gw,
            context,
            pack=out.pack(),
            settings=out.settings(gateway_context_tokens=context_tokens),
            bundle=bundle(),
            draft=draft,
            regenerate=regenerate,
            render=renders,
            numbers=out.numbers(),
            disclosure_sets={out.TERM: out.seed_set()},
            size_limited=size_limited,
        )
    return result, recorder, renders, regenerate, models


def size_verdicts(recorder: out.Recorder) -> list[tuple[str, str, str]]:
    return [r for r in recorder.rules() if r[1] == "RC-GUARD-SIZE"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["7" * 2521, emails_and_word(172)], ids=["digits", "emails"])
async def test_an_over_limit_turns_draft_is_dropped_before_any_rail_or_guard_call(
    monkeypatch: pytest.MonkeyPatch, text: str
) -> None:
    result, recorder, renders, regenerate, models = await released(monkeypatch, CLEAN, text)

    assert models.requests == []  # no output-mode guard call, no verify-claims
    assert result.kind == "fallback" and renders.seen == [None]  # the deterministic card
    assert regenerate.errors == []
    assert [r for r in recorder.rules() if r[0] != "release"] == []  # rails 6-7 never ran
    assert size_verdicts(recorder) == [("release", "RC-GUARD-SIZE", "fallback")]
    assert result.verdicts["release:RC-GUARD-SIZE"] == "fallback"
    assert result.verdicts["release:RC-GUARD"] == "pass"  # no model text to guard
    event = next(e for e in recorder.events if e["header"].rule_id == "RC-GUARD-SIZE")
    assert event["payload"] == {"detail": [f"masked size {LIMIT + 1} > {LIMIT}"]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "context_tokens"),
    [("7" * 2520, 8192), ("7" * 2521, None)],
    ids=["at-the-limit", "unset"],
)
async def test_otherwise_the_output_guard_carries_the_turn_as_before(
    monkeypatch: pytest.MonkeyPatch, text: str, context_tokens: int | None
) -> None:
    result, recorder, renders, _, models = await released(monkeypatch, CLEAN, text, context_tokens)

    assert result.kind == "narrative" and renders.seen == [CLEAN]
    output_mode = [m for r, m in models.requests if r == "guard-input"]
    assert len(output_mode) == 1
    assert [m["role"] for m in output_mode[0][1:]] == ["user", "assistant"]
    assert output_mode[0][1]["content"] == text
    assert size_verdicts(recorder) == [] and "release:RC-GUARD-SIZE" not in result.verdicts


@pytest.mark.asyncio
async def test_a_generation_the_caller_withheld_is_audited_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, recorder, renders, _, models = await released(
        monkeypatch, None, "7" * 2521, size_limited=True
    )

    assert models.requests == [] and renders.seen == [None] and result.kind == "fallback"
    assert size_verdicts(recorder) == [("release", "RC-GUARD-SIZE", "skipped")]


@pytest.mark.asyncio
async def test_a_turn_with_nothing_generated_is_released_exactly_as_without_the_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # S0 and every template turn: no draft, nothing withheld, so no new verdict and no change.
    on = await released(monkeypatch, None, "7" * 2521, 8192)
    off = await released(monkeypatch, None, "7" * 2521, None)

    assert size_verdicts(on[1]) == []
    assert (on[0].kind, on[0].text, on[0].verdicts) == (off[0].kind, off[0].text, off[0].verdicts)
    assert on[1].rules() == off[1].rules()


def files_text(log_dir: Path) -> str:
    return "".join(path.read_text() for path in log_dir.glob("*.log"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
async def test_a_dropped_draft_leaks_no_text_into_the_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", tmp_path)
    sentinel = "SENTINEL-CUSTOMER-TEXT"
    result, *_ = await released(monkeypatch, CLEAN, sentinel + " " + "7" * 2521)

    logged = capsys.readouterr().out + files_text(tmp_path)
    assert result.kind == "fallback" and "output-guard size limit" in logged
    assert sentinel not in logged and "7" * 20 not in logged and CLEAN not in logged
