"""State-0 (Step 18): the greeting, the AI disclosure, consent through the Consent Service only, the
typed path's closed lexicon, intent routing with the clarify-twice default, volunteered details,
re-entry, the identity question (I6) and the bundle's lexicons. The domain tier is a MockTransport:
the reference reads of runtime_support plus POST /v1/consent/records."""

import dataclasses
import json
import logging
import shutil
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
import yaml
from pydantic import ValidationError
from runtime_support import Models, domain, domain_handler, notice_json
from test_runtime_nodes import (
    BUNDLE,
    KEY,
    SCRIPTS,
    SID,
    Recorder,
    pipeline,
    rt,
    run,
    session,
    turn,
)

from surakshasetu.audit.events import EventType
from surakshasetu.compose.bundle import (
    PROMPT_BUNDLES,
    BundleError,
    ConsentLexicon,
    load_bundle,
    phrase,
)
from surakshasetu.domain.models import ConsentRecord, PurposeGrant
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import nodes
from surakshasetu.graph.handlers import identity
from surakshasetu.graph.nodes import Turn
from surakshasetu.graph.state import GraphState
from surakshasetu.graph.states import s0
from surakshasetu.logging import configure_logging
from surakshasetu.store import conv as store

CONSENT_ID = UUID("0199a1b2-0000-7000-8000-0000000000cc")
PURPOSES = {"P1": "P1_NEEDS_RECO", "P2": "P2_ADVISOR_CONTACT", "P3": "P3_MARKETING"}
HI = BUNDLE.templates["hi-IN"].scripts


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """Audit appends recorded, never written (a test may install its own Recorder)."""
    return Recorder(monkeypatch)


class ConsentService:
    """POST /v1/consent/records: the record the service would make, a 422 problem, or an outage.
    Every request is kept with its Idempotency-Key."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.fail = fail
        self.created: list[tuple[str, dict[str, Any]]] = []
        self.reads: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v1/consent/records":
            body = json.loads(request.content)
            self.created.append((request.headers["Idempotency-Key"], body))
            if self.fail == "down":
                raise httpx.ConnectError("down")
            if self.fail is not None:
                problem = {"type": "about:blank", "title": "x", "status": 422, "code": self.fail}
                return httpx.Response(422, json=problem)
            return httpx.Response(201, json=record_json(body))
        self.reads.append(request.url.path)
        if self.fail == "reads_down":
            raise httpx.ConnectError("down")
        return domain_handler(request)


def record_json(body: dict[str, Any]) -> dict[str, Any]:
    granted = {g["purpose_id"] for g in body["purposes"] if g["granted"]}
    adult = body["age_18_plus_declared"]
    return {
        "consent_id": str(CONSENT_ID),
        "notice_version": body["notice_version"],
        "notice_sha256": body["notice_sha256"],
        "notice_language": body["language"],
        "ai_disclosure_version": body["ai_disclosure_version"],
        "purposes": body["purposes"],
        "age_18_plus_declared": adult,
        "method": body["method"],
        "captured_at": datetime.now(UTC).isoformat(),
        "withdrawn_at": None,
        "valid_p1": adult and "P1_NEEDS_RECO" in granted,
        "valid_reasons": [] if adult else ["AGE_NOT_DECLARED"],
    }


def s0_turn(
    models: Models | None = None,
    *,
    text: str | None = "hello",
    action: dict[str, Any] | None = None,
    service: ConsentService | None = None,
    prompt: str | None = s0.PROMPT_CONSENT,
    record: ConsentRecord | None = None,
    **state: Any,
) -> Turn:
    """An S0 turn whose greeting was shown (prompt s0.consent) unless prompt says otherwise."""
    t = turn(models, text=None if action else text)
    t.action = action
    t.domain = domain(service or ConsentService())
    t.next = session(last_prompt_id=prompt, consent=record, **state)
    return t


def submit(p1: bool = True, p2: bool = True, p3: bool = True, adult: bool = True) -> dict[str, Any]:
    notice = notice_json()
    return {
        "type": "CONSENT_SUBMIT",
        "payload": {
            "purposes": {"P1": p1, "P2": p2, "P3": p3},
            "age_18_plus": adult,
            "notice_version": notice["notice_version"],
            "notice_sha256": notice["body_sha256"],
        },
    }


def valid_record(*purposes: str) -> ConsentRecord:
    notice = notice_json()
    return ConsentRecord.model_validate(
        record_json(
            {
                "notice_version": notice["notice_version"],
                "notice_sha256": notice["body_sha256"],
                "language": "en-IN",
                "ai_disclosure_version": "2026.09.1",
                "purposes": [
                    {"purpose_id": pid, "granted": p in (purposes or ("P1",))}
                    for p, pid in PURPOSES.items()
                ],
                "age_18_plus_declared": True,
                "method": "structured_action",
            }
        )
    )


def ids(t: Turn) -> list[str]:
    assert t.released is not None and t.released.rendered is not None
    return [i for i, _ in t.released.rendered.parts]


def transition(t: Turn) -> tuple[str, FsmState]:
    assert t.transition is not None
    return t.transition.row_id, t.transition.to


# --- the greeting ---------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize("action", [None, {"type": "START", "payload": {}}])
async def test_the_first_turn_is_the_greeting_with_the_ai_disclosure_and_the_notice(
    monkeypatch: pytest.MonkeyPatch, action: dict[str, Any] | None
) -> None:
    Recorder(monkeypatch)
    models = Models()
    t = s0_turn(models, action=action, prompt=None)

    await run(t)

    notice = notice_json()
    assert ids(t) == ["template:greeting", "registry:DISC-GLOBAL-AI-06", "notice:2026.09.1-en"]
    assert t.released is not None and t.released.text.startswith(
        "Namaste, I'm SurakshaSetu, an AI assistant from DUMMY Insurer. I'm not a human advisor."
    )
    assert notice["body"] in t.released.text
    form = t.form
    assert form is not None and form["type"] == "CONSENT_SUBMIT"
    assert (form["notice_version"], form["notice_sha256"]) == (
        notice["notice_version"],
        notice["body_sha256"],
    )
    assert [(p["id"], p["required"]) for p in form["purposes"]] == [
        ("P1", True),
        ("P2", False),
        ("P3", False),
    ]
    assert t.quick_replies == [
        {
            "label": SCRIPTS.consent_form.languages["hi-IN"],
            "action": {"type": "NOTICE_LANGUAGE", "payload": {"language": "hi-IN"}},
        }
    ]
    assert transition(t) == ("S0.STAY", FsmState.S0)
    assert t.next is not None and t.next.last_prompt_id == s0.PROMPT_CONSENT
    assert not any(r.startswith("gen-") for r in models.routes)  # S0 generates nothing
    body = nodes.response_body(t)
    assert body["message"]["form"] == form and body["message"]["quick_replies"] == t.quick_replies


# --- consent through the Consent Service ----------------------------------------------------------
@pytest.mark.asyncio
async def test_a_structured_consent_is_recorded_by_the_service_and_audited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    service = ConsentService()
    t = s0_turn(action=submit(), service=service)

    await run(t)

    ((key, body),) = service.created
    assert key == str(KEY)  # idempotent on the turn's key
    assert body["method"] == "structured_action" and body["age_18_plus_declared"] is True
    assert body["ai_disclosure_version"] == "2026.09.1" and body["language"] == "en-IN"
    assert all(g["granted"] for g in body["purposes"])
    (captured,) = [e for e in recorder.events if e["event_type"] is EventType.CONSENT_CAPTURED]
    header = captured["header"]
    assert header.purposes == ["P1", "P2", "P3"] and header.method == "structured_action"
    assert header.language == "en-IN" and header.adult_declared is True
    assert header.notice_version == "2026.09.1-en" and header.captured_at is not None
    assert t.next is not None and t.next.consent is not None and t.next.consent.valid_p1
    assert transition(t) == ("S0.STAY", FsmState.S0)
    assert ids(t) == ["template:intent_ask"]
    assert [q["action"]["payload"]["intent"] for q in t.quick_replies] == [
        "new_purchase",
        "specific_plan",
        "general_faq",
        "existing_policy",
    ]
    assert t.form is None


@pytest.mark.asyncio
async def test_p1_only_consent_records_p1_alone() -> None:
    service = ConsentService()
    t = s0_turn(action=submit(p2=False, p3=False), service=service)

    await run(t)

    granted = [g["purpose_id"] for g in service.created[0][1]["purposes"] if g["granted"]]
    assert granted == ["P1_NEEDS_RECO"]
    assert t.next is not None and t.next.consent is not None and t.next.consent.valid_p1


@pytest.mark.asyncio
async def test_p1_declined_exits_without_calling_the_service() -> None:
    service = ConsentService()
    t = s0_turn(action=submit(p1=False), service=service)

    await run(t)

    assert service.created == []
    assert transition(t) == ("S0.2", FsmState.EXIT)
    assert ids(t) == ["template:consent_declined"]
    assert t.form is None and t.quick_replies == []


@pytest.mark.asyncio
async def test_the_18_plus_box_unticked_erases_without_recording_anything() -> None:
    service = ConsentService()
    t = s0_turn(action=submit(adult=False), service=service)

    await run(t)

    assert service.created == []
    assert transition(t) == ("CC1b", FsmState.DATA_ERASURE)
    assert t.erasure == "MINOR" and ids(t) == ["template:minor_exit"]


@pytest.mark.asyncio
async def test_a_notice_mismatch_re_renders_the_notice_in_force() -> None:
    t = s0_turn(action=submit(), service=ConsentService(fail="NOTICE_MISMATCH"))

    await run(t)

    assert ids(t) == [
        "template:notice_updated",
        "registry:DISC-GLOBAL-AI-06",
        "notice:2026.09.1-en",
    ]
    assert t.form is not None and t.next is not None and t.next.consent is None
    assert transition(t) == ("S0.STAY", FsmState.S0)


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", ["down", "reads_down"])
async def test_a_consent_outage_offers_a_retry_and_never_proceeds(fail: str) -> None:
    t = s0_turn(action=submit(), service=ConsentService(fail=fail))
    if fail == "reads_down":  # the greeting itself cannot load its notice
        t.action = {"type": "START", "payload": {}}

    await run(t)

    assert ids(t) == ["template:consent_retry"]
    assert t.form is None
    assert t.quick_replies == [
        {"label": SCRIPTS.consent_form.retry, "action": {"type": "START", "payload": {}}}
    ]
    assert t.next is not None and t.next.consent is None
    assert transition(t) == ("S0.STAY", FsmState.S0)


@pytest.mark.asyncio
async def test_a_malformed_submit_re_presents_the_options() -> None:
    service = ConsentService()
    bad = submit()
    del bad["payload"]["purposes"]["P3"]
    t = s0_turn(action=bad, service=service)

    await run(t)

    assert service.created == [] and ids(t) == ["template:consent_reprompt"]
    assert t.form is not None


# --- the typed path -------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_typed_affirmation_asks_the_18_plus_question_then_records_p1_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    service = ConsentService()
    asked = s0_turn(text="  Haan. ", service=service)

    await run(asked)

    assert service.created == [] and ids(asked) == ["template:age_confirm_ask"]
    assert asked.next is not None and asked.next.last_prompt_id == s0.PROMPT_AGE

    confirmed = s0_turn(text="yes", service=service, prompt=s0.PROMPT_AGE)
    await run(confirmed)

    ((_, body),) = service.created
    assert body["method"] == "parsed_affirmation"
    assert [g["granted"] for g in body["purposes"]] == [True, False, False]
    header = next(
        e["header"] for e in recorder.events if e["event_type"] is EventType.CONSENT_CAPTURED
    )
    assert header.purposes == ["P1"] and header.method == "parsed_affirmation"
    assert ids(confirmed) == ["template:intent_ask"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said", "prompt", "row", "to"),
    [
        ("nahi", s0.PROMPT_AGE, "CC1b", FsmState.DATA_ERASURE),  # under 18 (V2)
        ("I don't agree", s0.PROMPT_CONSENT, "S0.2", FsmState.EXIT),
        ("मैं सहमत नहीं हूँ", s0.PROMPT_CONSENT, "S0.2", FsmState.EXIT),
    ],
)
async def test_typed_refusals_match_the_closed_lists(
    said: str, prompt: str, row: str, to: FsmState
) -> None:
    service = ConsentService()
    t = s0_turn(text=said, service=service, prompt=prompt)

    await run(t)

    assert service.created == [] and transition(t) == (row, to)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("said", "prompt"),
    [
        ("sure whatever, I accept everything", s0.PROMPT_CONSENT),
        ("i agree to everything", s0.PROMPT_CONSENT),
        ("maybe", s0.PROMPT_AGE),  # not a confirmation: back to the options
    ],
)
async def test_anything_else_is_not_consent(said: str, prompt: str) -> None:
    service = ConsentService()
    t = s0_turn(text=said, service=service, prompt=prompt)

    await run(t)

    assert service.created == [] and ids(t) == ["template:consent_reprompt"]
    assert t.next is not None and t.next.last_prompt_id == s0.PROMPT_CONSENT


@pytest.mark.asyncio
async def test_an_affirmation_before_the_prompt_is_shown_gets_the_greeting() -> None:
    t = s0_turn(text="i agree", prompt=None)

    await run(t)

    assert ids(t)[0] == "template:greeting"


@pytest.mark.asyncio
async def test_injection_in_the_consent_turn_is_ignored_and_counted() -> None:
    service = ConsentService()
    t = s0_turn(Models(injection=0.99), text="i agree", service=service)

    await run(t)

    assert service.created == [] and ids(t) == ["template:consent_reprompt"]
    assert t.next is not None and t.next.counters["injection"] == 1


# --- intent ---------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("intent", "row", "to"),
    [
        ("new_purchase", "S0.4", FsmState.S1),
        ("specific_plan", "S0.3", FsmState.QUOTE_ONLY),
        ("existing_policy", "S0.1", FsmState.HUMAN_ESCALATION),
        ("general_faq", "CC4", FsmState.S0),
    ],
)
async def test_the_intent_quick_replies_route_by_s0_rows(
    intent: str, row: str, to: FsmState
) -> None:
    t = s0_turn(
        action={"type": "INTENT", "payload": {"intent": intent}},
        prompt=s0.PROMPT_INTENT,
        record=valid_record("P1", "P2"),
    )
    t.from_state = FsmState.S0

    await run(t)

    assert transition(t) == (row, to)
    if to is FsmState.HUMAN_ESCALATION:  # from S0, contact options and no data shared, even with P2
        assert ids(t) == ["template:contact_options"]
    if to is FsmState.S0:
        assert ids(t) == ["template:side_query_caveat"]
    if to is not FsmState.S0:
        assert t.form is None


@pytest.mark.asyncio
async def test_free_text_intents_route_and_unclear_ones_are_clarified_twice_then_defaulted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    moved = s0_turn(Models(intents=("NEW_PURCHASE",)), text="I want to buy cover")
    moved.next = session(last_prompt_id=s0.PROMPT_INTENT, consent=valid_record())
    await run(moved)
    assert transition(moved) == ("S0.4", FsmState.S1)

    counters: dict[str, int] = {}
    for attempt in (1, 2):
        t = s0_turn(text="hmm", prompt=s0.PROMPT_INTENT, record=valid_record(), counters=counters)
        await run(t)
        assert ids(t) == ["template:clarify"] and transition(t) == ("S0.STAY", FsmState.S0)
        assert t.next is not None
        counters = t.next.counters
        assert counters[s0.CLARIFY] == attempt

    caplog.set_level(logging.INFO, logger="surakshasetu.graph.states.s0")
    third = s0_turn(text="hmm", prompt=s0.PROMPT_INTENT, record=valid_record(), counters=counters)
    await run(third)
    assert transition(third) == ("S0.4", FsmState.S1)
    assert "CONFIG_NOTE" in caplog.text


@pytest.mark.asyncio
async def test_an_intent_without_consent_does_not_move_the_session() -> None:
    t = s0_turn(action={"type": "INTENT", "payload": {"intent": "new_purchase"}})

    await run(t)

    assert transition(t) == ("S0.STAY", FsmState.S0)
    assert ids(t) == ["template:consent_reprompt"]


# --- nothing volunteered is filled ----------------------------------------------------------------
AGE = {"slot": "age_years", "value": 34, "confidence": 0.95, "evidence_span": "34 years old"}


@pytest.mark.asyncio
async def test_details_volunteered_before_consent_are_dropped_from_the_turn() -> None:
    t = s0_turn(Models(intents=("NEW_PURCHASE",), slots=(AGE,)), text="I'm 34 years old")

    await run(t)

    assert t.slots_pending == [] and t.slot_rows == []
    assert t.pipeline is not None and t.pipeline.analysis is not None
    assert t.pipeline.analysis.slots == []
    assert store.analysis_projection(t.pipeline.analysis)["slots"] == []  # type: ignore[index]
    assert t.volunteered == ["age_years"]
    assert transition(t) == ("S0.STAY", FsmState.S0)


@pytest.mark.asyncio
async def test_a_stated_age_under_18_erases() -> None:
    minor = {"slot": "age_years", "value": 16, "confidence": 0.9, "evidence_span": "16 years old"}
    t = s0_turn(Models(slots=(minor,)), text="I'm 16 years old")

    await run(t)

    assert transition(t) == ("CC1b", FsmState.DATA_ERASURE) and t.erasure == "MINOR"


# --- language, re-entry, identity -----------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_notice_language_switch_renders_the_hindi_notice_and_re_pins_at_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = Recorder(monkeypatch)
    t = s0_turn(action={"type": "NOTICE_LANGUAGE", "payload": {"language": "hi-IN"}})

    await run(t)

    assert t.next is not None and t.next.locale == "hi-IN"
    assert ids(t) == ["template:greeting", "registry:DISC-GLOBAL-AI-06", "notice:2026.09.1-hi"]
    assert t.released is not None and t.released.text.startswith(HI.greeting.split("{")[0])
    assert t.form is not None and t.form["language"] == "hi-IN"
    assert t.quick_replies[0]["action"]["payload"] == {"language": "en-IN"}

    hindi = notice_json("hi-IN")
    consented = s0_turn(action=submit(), locale="hi-IN")
    consented.action["payload"] |= {  # type: ignore[index]
        "notice_version": hindi["notice_version"],
        "notice_sha256": hindi["body_sha256"],
    }
    await run(consented)

    assert consented.next is not None
    assert consented.next.pins.consent_notice == "2026.09.1-hi"  # I7's exception (D1)
    captured = next(e for e in recorder.events if e["event_type"] is EventType.CONSENT_CAPTURED)
    assert captured["pins"]["consent_notice"] == "2026.09.1-hi"
    assert captured["header"].notice_version == "2026.09.1-hi"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("record", "expected"),
    [
        ("valid", ["template:intent_ask"]),
        ("lapsed", ["template:consent_renew", "registry:DISC-GLOBAL-AI-06", "notice:2026.09.1-en"]),
        (None, ["template:greeting", "registry:DISC-GLOBAL-AI-06", "notice:2026.09.1-en"]),
    ],
)
async def test_entering_s0_opens_the_prompt_for_the_consent_held(
    record: str | None, expected: list[str]
) -> None:
    held = {
        "valid": valid_record(),
        "lapsed": valid_record().model_copy(
            update={"valid_p1": False, "valid_reasons": ["NOTICE_SUPERSEDED"]}
        ),
        None: None,
    }[record]
    t = s0_turn(record=held, prompt=None)

    await s0.enter(GraphState(), runtime=rt(t))
    await nodes.compose(GraphState(), rt(t))

    assert t.render is not None
    assert [i for i, _ in t.render(None).parts] == expected


@pytest.mark.asyncio
async def test_an_identity_question_gets_the_templated_re_disclosure_in_any_state() -> None:
    before = s0_turn(text="Hi, are you a real person?")
    await run(before)
    assert ids(before) == ["template:ai_redisclosure"]
    assert before.released is not None
    assert "DUMMY: AI assistant identity and human alternative (en-IN)." in before.released.text
    assert before.form is not None  # the consent prompt stays open

    later = s0_turn(text="kya aap insaan ho?", record=valid_record())
    later.next = session(fsm_state=FsmState.S1, consent=valid_record())
    await run(later)
    assert ids(later) == ["template:ai_redisclosure"]  # not the stub's advisor_offer


@pytest.mark.asyncio
async def test_the_re_disclosure_stays_truthful_when_the_registry_is_down() -> None:
    t = s0_turn(text="are you a bot", service=ConsentService(fail="reads_down"))
    t.next = session(fsm_state=FsmState.S1, consent=valid_record())

    await run(t)

    assert t.released is not None
    assert (
        t.released.text
        == "I'm SurakshaSetu, an AI assistant from DUMMY Insurer. I'm not a human advisor."
    )


def test_the_identity_lexicon_matches_whole_words_only() -> None:
    def asks(text: str) -> bool:
        return identity.asks(BUNDLE, dataclasses.replace(pipeline(None), stored_raw=text))

    assert asks("Are you a human?") and asks("ARE YOU   HUMAN") and asks("क्या आप इंसान हैं?")
    assert not asks("are you humane") and not asks("is this a botanical garden")
    assert not identity.asks(BUNDLE, None)  # an action turn has no text


# --- the decide contract and the lexicon ----------------------------------------------------------
@pytest.mark.asyncio
async def test_a_signal_that_is_not_a_facts_field_fails_the_turn() -> None:
    t = s0_turn()
    await nodes.input_node(GraphState(), rt(t))
    await nodes.route(GraphState(), rt(t))
    t.signals["consented"] = True

    with pytest.raises(ValidationError):
        await nodes.decide(GraphState(), rt(t))


def test_lexicon_entries_and_turns_are_compared_normalised() -> None:
    assert phrase("  Main  Sahmat   HOON! ") == "main sahmat hoon"
    assert phrase("ｉ ａｇｒｅｅ।") == "i agree"  # NFKC, and a final danda dropped
    lexicon = BUNDLE.consent_lexicon
    assert "main sahmat hoon" in lexicon.affirm and "हाँ" in lexicon.adult
    assert lexicon.affirm.isdisjoint(lexicon.decline) and lexicon.adult.isdisjoint(lexicon.minor)


def test_the_lexicon_refuses_unquoted_yes_and_overlapping_lists() -> None:
    with pytest.raises(ValidationError):
        ConsentLexicon.model_validate(
            yaml.safe_load("{affirm: [yes], decline: [], adult: [], minor: []}")
        )
    with pytest.raises(ValidationError):
        ConsentLexicon.model_validate(
            {"affirm": ["ok"], "decline": ["OK"], "adult": [], "minor": []}
        )


def test_the_manifest_binds_the_lexicons(tmp_path: Any) -> None:
    copy = tmp_path / BUNDLE.version
    shutil.copytree(PROMPT_BUNDLES / BUNDLE.version, copy)
    (copy / "lexicons" / "identity_question.yaml").write_text("phrases: [yes]\n")
    with pytest.raises(BundleError) as refused:
        load_bundle(BUNDLE.version, env="dev", root=tmp_path)
    assert refused.value.reason == "HASH_MISMATCH"  # the manifest binds the lexicons too


# --- logs -----------------------------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
async def test_s0_logs_no_customer_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", tmp_path)
    Recorder(monkeypatch)
    words = "I'm 16 years old, my PAN is ABCDE1234F, haan"
    minor = {"slot": "age_years", "value": 16, "confidence": 0.9, "evidence_span": "16 years old"}
    t = s0_turn(Models(slots=(minor,)), text=words)
    consented = s0_turn(action=submit())

    await run(t)
    await run(consented)

    logged = capsys.readouterr().out + "".join(p.read_text() for p in tmp_path.glob("*.log"))
    assert "under 18 stated in S0" in logged and "consent captured" in logged
    for leaked in ("16 years", "ABCDE1234F", "haan", str(CONSENT_ID), str(SID)):
        assert leaked not in logged


def test_purpose_grants_use_the_contract_names() -> None:
    assert [
        PurposeGrant(purpose_id=pid, granted=True).purpose_id for pid in s0.PURPOSES.values()
    ] == [
        "P1_NEEDS_RECO",
        "P2_ADVISOR_CONTACT",
        "P3_MARKETING",
    ]
