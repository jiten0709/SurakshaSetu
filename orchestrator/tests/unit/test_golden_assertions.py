"""The golden harness's own checks, offline: every conversation file validates, and each global
assertion catches a planted violation. Several can't be tripped by a conversation yet (I2, I4,
numbers, S0 generation), and this file is what proves they aren't vacuous."""

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "golden"))
from harness import (  # noqa: E402
    Conversation,
    Event,
    Sent,
    Transcript,
    Turn,
    check,
    load_conversations,
    sha256_text,
)

from surakshasetu.crypto.jcs import sha256_hex  # noqa: E402

PINS = {"prompt_bundle": "pb-2026.10.5", "rules": "2026.09.1"}


def body(text: str = "Hi", state: str = "S0", turn_id: str = "t1", **message: Any) -> bytes:
    released = {
        "turn_id": turn_id,
        "state": state,
        "message": {
            "text": text,
            "parts": [{"id": "template:advisor_offer", "text": text}],
            "citations": [],
            "sources": [],
            "disclosures": [],
            "cta": None,
            "quick_replies": [],
        }
        | message,
        "documents": [],
    }
    return json.dumps(released).encode()


def events(raw: bytes, *, pins: dict[str, Any] = PINS, first_seq: int = 1) -> list[Event]:
    released = json.loads(raw)
    return [
        Event(first_seq, "TURN_INPUT", {"turn_id": released["turn_id"]}, pins),
        Event(
            first_seq + 1,
            "STATE_TRANSITION",
            {
                "from_state": "S0",
                "to_state": released["state"],
                "trigger": "S0.STAY",
                "invariants": {},
            },
            pins,
        ),  # fmt: skip
        Event(
            first_seq + 2,
            "RESPONSE_RELEASED",
            {
                "rendered_sha256": sha256_text(released["message"]["text"]),
                "disclosure_set_sha256s": sorted(
                    d["set_sha256"] for d in released["message"]["disclosures"]
                ),
            },
            pins,
            {"response": released},
        ),
    ]


def transcript(*turns: dict[str, Any], **update: Any) -> Transcript:
    conversation = Conversation.model_validate(
        {"id": "planted", "description": "x", "turns": list(turns) or [{"text": "hello"}]}
    )
    raw = body()
    t = Transcript(
        conversation=conversation,
        active_bundle="pb-2026.10.5",
        consent_valid_from_start=False,
        initial_pins=PINS,
        sent=[Sent(0, 200, raw, customer_text="hello")],
        events=events(raw),
        chain_ok=True,
        chain_checked=3,
        products={"999N001V02": "Suraksha Term Shield"},
    )
    for name, value in update.items():
        setattr(t, name, value)
    return t


def test_a_clean_transcript_passes() -> None:
    assert check(transcript()) == []


def test_the_chain_must_verify() -> None:
    assert check(transcript(chain_ok=False))[0].startswith("chain:")
    assert check(transcript(chain_checked=2))[0].startswith("chain:")


def test_i1_no_decision_or_slot_before_valid_consent() -> None:
    t = transcript()
    t.events.insert(1, Event(9, "ENGINE_DECISION", {"service": "eligibility"}, PINS))
    assert any(f.startswith("I1: ENGINE_DECISION") for f in check(t))
    t.consent_valid_from_start = True
    assert not any(f.startswith("I1") for f in check(t))
    assert any(f.startswith("I1: 2 slot rows") for f in check(transcript(slots_without_consent=2)))


def test_i2_s3_needs_a_current_suitability_decision() -> None:
    t = transcript()
    entry = {"from_state": "S2", "to_state": "S3", "trigger": "S2.2", "invariants": {"I2": True}}
    t.events.append(Event(4, "STATE_TRANSITION", entry, PINS))
    assert any(f.startswith("I2") for f in check(t))  # no suitability decision before it
    t.events.insert(0, Event(0, "ENGINE_DECISION", {"service": "suitability"}, PINS))
    assert not any(f.startswith("I2") for f in check(t))
    t.events[-1] = Event(4, "STATE_TRANSITION", entry | {"invariants": {"I2": False}}, PINS)
    assert any(f.startswith("I2") for f in check(t))


def suitability_decision(outcome: str = "FIT", **tamper: Any) -> Event:
    """A suitability ENGINE_DECISION as S2 records it (Step 20): the needs as sent, with their
    slots_sha256, and the result echoing the hash."""
    needs = {"goals": ["income_protection"], "annual_income_inr": "1200000", "income_type": "x"}
    digest = sha256_hex(needs)
    request = {"needs": needs | {"slots_sha256": tamper.get("sent", digest)}}
    result = {"outcome": outcome, "inputs_sha256": tamper.get("result", digest)}
    header = {"service": "suitability", "inputs_sha256": tamper.get("header", digest)}
    return Event(0, "ENGINE_DECISION", header, PINS, {"request": request, "result": result})


@pytest.mark.parametrize(
    ("decision", "fails"),
    [
        (suitability_decision(), False),
        (suitability_decision(sent="0" * 64), True),  # bound to other needs
        (suitability_decision(result="1" * 64), True),  # the engine decided other inputs
        (suitability_decision(header="2" * 64), True),
        (suitability_decision("NO_GAP"), True),  # S3 on a decision that wasn't FIT
    ],
)
def test_i2_s3_needs_the_decision_bound_to_the_needs_it_was_asked_for(
    decision: Event, fails: bool
) -> None:
    t = transcript()
    entry = {"from_state": "S2", "to_state": "S3", "trigger": "S2.2", "invariants": {"I2": True}}
    t.events = [decision, *t.events, Event(4, "STATE_TRANSITION", entry, PINS)]
    assert any(f.startswith("I2") for f in check(t)) is fails


def test_a_prelude_puts_its_turns_first(tmp_path: Path) -> None:
    conversations, preludes = tmp_path / "conversations", tmp_path / "preludes"
    (conversations / "suite").mkdir(parents=True)
    preludes.mkdir()
    (preludes / "opening.yaml").write_text("- {action: {type: START}}\n")
    convo = "{id: with-prelude, description: x, prelude: opening, turns: [{text: hi}]}\n"
    (conversations / "suite" / "c.yaml").write_text(convo)
    (loaded,) = load_conversations(conversations, preludes)
    assert [t.action.type if t.action else t.text for t in loaded.turns] == ["START", "hi"]
    (conversations / "suite" / "c.yaml").write_text(convo.replace("opening", "missing"))
    with pytest.raises(FileNotFoundError):
        load_conversations(conversations, preludes)


@pytest.mark.parametrize("leak", ["Our 999N001V02 fits", "the Suraksha Term Shield fits"])
def test_i3_no_product_before_s3_unless_the_customer_named_it(leak: str) -> None:
    raw = body(leak)
    t = transcript(sent=[Sent(0, 200, raw, customer_text="hello")], events=events(raw))
    assert any(f.startswith("I3") for f in check(t))
    t.sent[0].customer_text = leak  # the customer named it first
    assert not any(f.startswith("I3") for f in check(t))
    s3 = body(leak, state="S3")
    assert not any(
        f.startswith("I3")
        for f in check(transcript(sent=[Sent(0, 200, s3, customer_text="hi")], events=events(s3)))
    )


def test_i3_a_product_the_customer_named_may_be_shown_by_its_name_or_uin() -> None:
    """Step 19 (decided 2026-10-04): the Quote-Only card shows the named plan's name and UIN."""
    card = body("Indicative premium for Suraksha Term Shield (UIN 999N001V02)")
    for said in ("price the suraksha term shield", "quote 999n001v02"):
        t = transcript(sent=[Sent(0, 200, card, customer_text=said)], events=events(card))
        assert not any(f.startswith("I3") for f in check(t)), said
    other = body("Indicative premium for Suraksha Term Shield (UIN 999N001V02), or 999N002V01")
    t = transcript(
        sent=[Sent(0, 200, other, customer_text="price the Suraksha Term Shield")],
        events=events(other),
    )
    assert any("999N002V01" in f for f in check(t))  # another product still leaks


def test_i4_s3_disclosure_hashes_are_the_registrys() -> None:
    shown = [{"uin": "999N001V02", "set_sha256": "ab" * 32}]
    raw = body(state="S3", disclosures=shown)
    t = transcript(sent=[Sent(0, 200, raw)], events=events(raw), registry={"999N001V02": "cd" * 32})
    assert any(f.startswith("I4: turn 0 999N001V02") for f in check(t))
    t.registry = {"999N001V02": "ab" * 32}
    assert not any(f.startswith("I4") for f in check(t))
    t.events[2] = Event(
        3, "RESPONSE_RELEASED", t.events[2].header | {"disclosure_set_sha256s": []}, PINS
    )
    assert any("RESPONSE_RELEASED disclosure hashes" in f for f in check(t))


def withdrawal(consent_withdrawal: str, template: str, *, erased: bool = True) -> Transcript:
    turn = {"text": "delete my data", "script": {"nlu-extract": [{"intents": ["META_WITHDRAW"]}]}}
    raw = body("Done", state="DATA_ERASURE", parts=[{"id": f"template:{template}", "text": "Done"}])
    recorded = events(raw)
    request = {"reason_code": "WITHDRAW", "consent_withdrawal": consent_withdrawal}
    recorded.insert(2, Event(9, "ERASURE_REQUEST", request, PINS))
    return transcript(turn, sent=[Sent(0, 200, raw)], events=recorded, erased=erased,
                      chain_checked=4)  # fmt: skip


def test_i5_a_withdrawal_is_audited_erased_and_reported_truthfully() -> None:
    assert check(withdrawal("done", "erasure_done")) == []
    assert check(withdrawal("pending", "erasure_pending")) == []
    assert any("does not match" in f for f in check(withdrawal("pending", "erasure_done")))
    assert any("does not match" in f for f in check(withdrawal("done", "erasure_pending")))
    assert check(withdrawal("pending", "minor_exit")) == []  # the exit claims no withdrawal
    assert any("live rows" in f for f in check(withdrawal("done", "erasure_done", erased=False)))
    unaudited = withdrawal("done", "erasure_done")
    unaudited.events = [e for e in unaudited.events if e.event_type != "ERASURE_REQUEST"]
    unaudited.chain_checked = 3
    assert any("no ERASURE_REQUEST" in f for f in check(unaudited))
    unasked = withdrawal("done", "erasure_done")
    unasked.conversation = transcript().conversation
    assert any("without a request" in f for f in check(unasked))


def identity_reply(*parts: str) -> list[str]:
    t = transcript({"text": "are you a human?", "identity": True})
    raw = body(parts=[{"id": i, "text": "Hi"} for i in parts])
    t.sent, t.events = [Sent(0, 200, raw)], events(raw)
    return [f for f in check(t) if f.startswith("I6")]


def test_i6_an_identity_question_gets_the_re_disclosure_first_and_fixed_text_only() -> None:
    assert any(f.startswith("I6") for f in check(transcript({"text": "x", "identity": True})))
    assert identity_reply("template:ai_redisclosure") == []
    assert identity_reply("template:safety", "template:ai_redisclosure") == []
    assert identity_reply("template:ai_redisclosure", "template:greeting", "notice:v1") == []
    assert identity_reply("template:greeting", "template:ai_redisclosure")  # not first
    assert identity_reply("template:ai_redisclosure", "narrative")  # generated text alongside


def test_i7_pins_change_only_by_a_bundle_kill_switch_or_a_re_consent() -> None:
    raw = body()
    moved = transcript(events=events(raw, pins=PINS | {"rules": "2026.10.1"}))
    assert any(f.startswith("I7: pins ['rules']") for f in check(moved))
    repinned = transcript(initial_pins=PINS | {"prompt_bundle": "pb-2026.09.1"})
    assert any(f.startswith("I7") for f in check(repinned))  # no kill switch recorded
    repinned.switched_bundles = {"pb-2026.09.1"}
    assert check(repinned) == []

    old, new = PINS | {"consent_notice": "n-en"}, PINS | {"consent_notice": "n-hi"}
    captured = Event(3, "CONSENT_CAPTURED", {"notice_version": "n-hi"}, new)

    def notice_moved(at: Event) -> list[str]:
        t = transcript(initial_pins=old, events=events(raw, pins=old)[:2])
        t.events += [at, *events(raw, pins=new, first_seq=4)[1:]]
        return [f for f in check(t) if f.startswith("I7")]

    assert notice_moved(captured) == []  # moved by the capture that names it (D1)
    other = Event(3, "CONSENT_CAPTURED", {"notice_version": "n-xx"}, new)
    assert notice_moved(other)  # a capture naming another notice
    assert notice_moved(Event(3, "GUARD_VERDICT", {}, new))  # moved without a capture


def test_s0_calls_no_generation_route() -> None:
    def generated(state: str) -> list[str]:
        t = transcript()
        t.events.insert(2, Event(9, "MODEL_CALL", {"route": "gen-converse"}, PINS, None, state))
        t.chain_checked = len(t.events)
        return [f for f in check(t) if f.startswith("S0:")]

    assert generated("S0") and generated("S1") == []
    t = transcript()
    t.events.insert(2, Event(9, "MODEL_CALL", {"route": "nlu-extract"}, PINS, None, "S0"))
    t.chain_checked = len(t.events)
    assert check(t) == []  # turn analysis is not generation


def test_the_consent_prompt_is_released_verbatim_with_a_form_for_a_held_notice() -> None:
    form = {"notice_version": "n1", "notice_sha256": "ab" * 32}
    parts = [
        {"id": "template:greeting", "text": "Hi"},
        {"id": "registry:DISC-GLOBAL-AI-06", "text": "AI body"},
        {"id": "notice:n1", "text": "Notice body"},
    ]

    def released(**update: Any) -> list[str]:
        held = {"notices": {"n1": ("Notice body", "ab" * 32)}, "registry_bodies": {"AI body"}}
        raw = body(parts=parts, form=form)
        t = transcript(sent=[Sent(0, 200, raw)], events=events(raw), **(held | update))
        return [f for f in check(t) if f.startswith("consent:")]

    assert released() == []
    assert released(notices={"n1": ("Another body", "ab" * 32)})  # the notice part
    assert released(notices={"n1": ("Notice body", "cd" * 32)})  # the form's hash
    assert released(notices={})  # a notice the service does not hold
    assert released(registry_bodies={"Another"})  # the registry part

    # Step 19: the indicative quote's DISC-GLOBAL-QUOTE-02 is a registry part too.
    parts.append({"id": "registry:DISC-GLOBAL-QUOTE-02", "text": "Quote body"})
    assert released(registry_bodies={"AI body", "Quote body"}) == []
    assert released()  # a body the registry does not hold


def test_i8_every_delivery_is_a_committed_release() -> None:
    t = transcript()
    t.events[2] = Event(
        3,
        "RESPONSE_RELEASED",
        t.events[2].header | {"rendered_sha256": "0" * 64},
        PINS,
        t.events[2].payload,
    )
    assert any("not the committed hash" in f for f in check(t))

    t = transcript()
    t.events[2] = Event(3, "RESPONSE_RELEASED", t.events[2].header, PINS, {"response": {}})
    assert any("not the committed payload" in f for f in check(t))

    t = transcript()
    t.events.append(Event(4, "RESPONSE_RELEASED", {"rendered_sha256": "0" * 64}, PINS))
    t.chain_checked = 4
    assert any("2 releases committed, 1 delivered" in f for f in check(t))

    t = transcript({"text": "hello"}, {"retry": True})
    t.sent.append(Sent(1, 200, body("Hello"), replay_of=0))
    assert any("replay differs" in f for f in check(t))
    t.sent[1] = Sent(1, 200, t.sent[0].body, replay_of=0)
    assert check(t) == []


def test_raw_pii_is_in_no_header_redacted_text_log_or_briefing() -> None:
    typed = "my PAN is ABCDE1234F"
    for place in ("logs", "redacted", "briefings", "events"):
        t = transcript()
        t.sent[0].customer_text = typed
        if place == "logs":
            t.logs = "turn released ABCDE1234F"
        elif place == "redacted":
            t.redacted = ["my PAN is ABCDE1234F"]
        elif place == "briefings":
            t.briefings = [{"profile": {"note": "ABCDE1234F"}}]
        else:
            t.events[0] = Event(1, "TURN_INPUT", {"turn_id": "t1", "leak": "ABCDE1234F"}, PINS)
        assert [f for f in check(t) if f.startswith("PII")], place
    clean = transcript()
    clean.sent[0].customer_text, clean.redacted = typed, ["my PAN is <PAN_1>"]
    assert check(clean) == []


def test_released_numbers_come_from_engine_values_or_cited_evidence() -> None:
    parts = [{"id": "why_it_fits", "text": "cover of 5,00,000 over 30 years"}]
    raw = body(parts=parts)
    t = transcript(sent=[Sent(0, 200, raw)], events=events(raw))
    assert any(f.startswith("numbers") for f in check(t))
    t.events.insert(
        1, Event(9, "ENGINE_DECISION", {"service": "ranking"}, PINS, {"sa": "5,00,000"})
    )
    t.consent_valid_from_start, t.chain_checked = True, 4
    assert any("['30']" in f for f in check(t))  # 5,00,000 now sourced; 30 still is not
    t.evidence = {"product:x:1:abc": "a policy term of 30 years"}
    assert check(t) == []
    templated = body("call 1800 000 000")  # a template's own numbers are approved text
    assert check(transcript(sent=[Sent(0, 200, templated)], events=events(templated))) == []


def test_an_engine_amount_may_read_as_a_filled_placeholder_and_s3_template_parts_are_fixed() -> (
    None
):
    """Step 21: why_it_fits carries ₹1,06,875 where the engine's JSON says 106875; the composer's
    own parts (cards, comparison, sources) are filled from the engines and the catalog."""
    parts = [{"id": "why_it_fits", "text": "about ₹1,06,875 a year [Source: PW §3]"}]
    raw = body(parts=parts)
    t = transcript(sent=[Sent(0, 200, raw)], events=events(raw))
    t.events.insert(1, Event(9, "ENGINE_DECISION", {"service": "quote"}, PINS, {"p": "106875"}))
    t.consent_valid_from_start, t.chain_checked = True, 4
    assert any("['3']" in f for f in check(t))  # the label's number needs its evidence
    t.evidence = {"product:x:3:abc": "DUMMY: the exclusions. PW §3"}
    assert check(t) == []
    t.events[1] = Event(9, "ENGINE_DECISION", {"service": "quote"}, PINS, {"p": "106874"})
    assert any("1,06,875" in f for f in check(t))
    fixed = [
        {"id": "option_card:999N001V02", "text": "Cover: ₹3,75,00,000 for 26 years"},
        {"id": "comparison", "text": "| Entry age (years) | 18–65 |"},
        {"id": "sources", "text": "Policy Wording, version v2, effective 1 Sep 2026"},
    ]
    raw = body(parts=fixed)
    assert check(transcript(sent=[Sent(0, 200, raw)], events=events(raw))) == []


def handed_off(*, ack: str | None = "s" * 64, valid_until: str = "2099-01-01") -> Transcript:
    """A transcript whose last turn hands off: the quote, the acknowledgment, the intake, S3.4."""
    t = transcript(registry={"999N001V02": "s" * 64})
    planted = [
        Event(10, "ENGINE_DECISION", {"service": "quote"}, PINS,
              {"result": {"quote_id": "Q-1", "valid_until": valid_until}}),
        *([Event(11, "DISCLOSURE_ACK", {"uin": "999N001V02", "set_sha256": ack}, PINS)]
          if ack else []),
        Event(12, "HANDOFF", {"reason_code": "APPLICATION_INTAKE", "queue": "application"}, PINS,
              {"intake": {"selected": {"uin": "999N001V02", "quote_id": "Q-1"}},
               "intake_ref": "I"}),
        Event(13, "STATE_TRANSITION", {"from_state": "S3", "to_state": "HANDOFF",
                                       "trigger": "S3.4", "invariants": {}}, PINS),
    ]  # fmt: skip
    t.events += planted
    return t


def v7(t: Transcript) -> list[str]:
    return [f for f in check(t) if f.startswith("V7")]


def test_a_handoff_rests_on_an_ack_of_the_registry_set_and_a_valid_quote() -> None:
    assert v7(handed_off()) == []
    assert v7(handed_off(ack=None)) == ["V7: HANDOFF at seq 13 without an acknowledgment"]
    assert "not of the registry set" in v7(handed_off(ack="0" * 64))[0]
    assert "not issued or not valid" in v7(handed_off(valid_until="2020-01-01"))[0]
    no_intake = handed_off()
    no_intake.events = [e for e in no_intake.events if e.event_type != "HANDOFF"]
    assert v7(no_intake) == ["V7: HANDOFF at seq 13 without an intake sent"]
    ranked = handed_off()  # an option's own quote, from the ranking decision
    ranked.events[3] = Event(10, "ENGINE_DECISION", {"service": "ranking"}, PINS, {"result": {
        "options": [{"quote": {"quote_id": "Q-1", "valid_until": "2099-01-01"}}]}})  # fmt: skip
    assert v7(ranked) == []
    stays = handed_off(ack=None)  # a turn after the hand-off stays; only the entry is checked
    stay = {"from_state": "HANDOFF", "to_state": "HANDOFF", "trigger": "HANDOFF.STAY"}
    stays.events.append(Event(14, "STATE_TRANSITION", stay | {"invariants": {}}, PINS))
    assert len(v7(stays)) == 1


# --- the conversation files -----------------------------------------------------------------------
def test_every_conversation_file_validates_and_core_covers_the_step_17_paths() -> None:
    conversations = {c.id: c for c in load_conversations()}

    assert len(conversations) >= 20
    withdrawals = [c for c in conversations.values() if any(t.withdraws for t in c.turns)]
    states = {c.given.state.value if c.given.state else "S0" for c in withdrawals}
    assert {"S0", "S1", "QUOTE_ONLY", "S2", "S3", "PAUSE"} <= states
    for c in withdrawals:  # every withdrawal expects its erasure
        assert any(t.withdraws and t.expect.erased for t in c.turns), c.id
    flags = {f for c in conversations.values() for t in c.turns
             for f in ("retry", "concurrent", "checkpoint_lost") if getattr(t, f)}  # fmt: skip
    assert flags == {"retry", "concurrent", "checkpoint_lost"}
    assert any(t.kill_switch for c in conversations.values() for t in c.turns)


def test_the_s0_suite_covers_the_step_18_paths() -> None:
    suite = [c for c in load_conversations() if c.id.startswith("s0-")]
    turns = [t for c in suite for t in c.turns]

    assert len(suite) >= 14
    methods = {t.expect.consent.method for t in turns if t.expect.consent} - {None}
    assert methods == {"structured_action", "parsed_affirmation"}
    assert any(t.identity for t in turns) and any(t.notice_bump for t in turns)
    assert any(t.expect.erased == "destroyed" for t in turns)  # under 18
    states = {t.expect.state.value for t in turns if t.expect.state}
    assert {"S0", "S1", "QUOTE_ONLY", "EXIT", "HUMAN_ESCALATION", "DATA_ERASURE"} <= states
    assert any(c.locale == "en-IN" and any(
        t.action and t.action.type == "NOTICE_LANGUAGE" for t in c.turns) for c in suite
    )  # fmt: skip
    for c in suite:  # S0 starts clean: consent comes only through S0's own turns
        assert c.given.consent is None or c.given.state is not None, c.id


def test_the_s1_and_quote_only_suites_cover_the_step_19_paths() -> None:
    every = load_conversations()
    s1 = [c for c in every if c.id.startswith("s1-")]
    quote_only = [c for c in every if c.id.startswith("qo-")]
    assert len(s1) >= 14 and len(quote_only) >= 8
    reasons = {t.expect.reason for c in s1 for t in c.turns}
    assert {
        "ELIGIBLE",
        "HE_NRI",
        "HE_AGE_BAND",
        "HE_COMPLEX_PROPOSER",
        "HE_RE_ASK_LIMIT",
        "NOT_ELIGIBLE",
        "EXPRESS_PATH",
        "CORRECTION_ELIGIBILITY",
    } <= reasons
    assert any(
        t.expect.erased == "destroyed" and t.expect.slot_rows == 0 for c in s1 for t in c.turns
    )
    assert {"QO.1", "QO.2", "QO.3", "QO.3b"} <= {t.expect.row for c in quote_only for t in c.turns}
    for c in s1 + quote_only:  # reached through real S0 turns, never seeded
        assert c.given.consent is None and c.given.state is None, c.id


def test_the_s2_suite_covers_the_step_20_paths() -> None:
    s2 = [c for c in load_conversations() if c.id.startswith("s2-")]
    assert len(s2) >= 14
    turns = [t for c in s2 for t in c.turns]
    assert {"S2.1", "S2.1b", "S2.2", "G3"} <= {t.expect.row for t in turns}
    assert {
        "NO_GAP",
        "HE_AFFORDABILITY_RED",
        "HE_VULNERABLE_COMPLEX",
        "HE_OUT_OF_SCOPE",
        "SUITABLE",
        "CORRECTION_NEEDS",
    } <= {t.expect.reason for t in turns}
    templates = {i for t in turns for i in (t.expect.templates or [])}
    assert {
        "period_ask",
        "short_path_offer",
        "summary_assumed",
        "summary_implausible",
        "partial_offer",
        "amber_confirm",
        "distress_ack",
        "non_earning_basis",
        "guarantee_note",
        "competitor_note",
    } <= templates
    assert any("SUFFICIENCY_ELECTION" in t.expect.events for t in turns)
    for c in s2:  # reached through real S0 and S1 turns, never seeded
        assert c.prelude == "to-s2" and c.given.consent is None and c.given.state is None, c.id


def test_the_s3_suite_and_the_scripted_conversation_cover_the_step_21_paths() -> None:
    every = {c.id: c for c in load_conversations()}
    scripted = every["scripted-s0-s3"]
    assert scripted.prelude == "to-s3" and scripted.turns[-1].expect.intake
    assert scripted.turns[-1].expect.state is not None
    assert scripted.turns[-1].expect.state.value == "HANDOFF"
    s3 = [c for c in every.values() if c.id.startswith("s3-")]
    assert len(s3) >= 16
    turns = [t for c in s3 for t in c.turns]
    assert {"S3.1", "S3.2", "S3.3", "S3.3b", "S3.4", "PAUSE.R", "CC5"} <= {
        t.expect.row for t in turns
    }
    templates = {i for t in turns for i in (t.expect.templates or [])}
    assert {
        "alternatives",
        "gap_choice",
        "rerank_note",
        "requote_note",
        "which_one",
        "not_recommended",
        "ack_mismatch",
        "journey_down",
        "apply_needs_premium",
        "release_blocked",
        "s3_summary",
        "rediscovery",
        "decline_first",
        "declined_exit",
    } <= templates
    parts = {i for t in turns for i in (t.expect.parts or [])}
    assert {"template:exclusion_note", "template:tax_condition", "registry:DISC-GLOBAL-TAX-05",
            "template:guarantee_note"} <= parts  # fmt: skip
    assert any(t.registry_tamper for t in turns) and any(t.journey_down for t in turns)
    assert any(t.days_later for t in turns) and any(c.given.fixture_product for c in s3)
    assert any(t.kill_switch and t.kill_switch.kind == "product" for t in turns)


@pytest.mark.parametrize(
    "turn",
    [
        {"text": "a", "delete": True},  # two requests
        {},  # none
        {"concurrent": True, "retry": True},  # concurrent needs text
        {"text": "a", "script": {"gen-everything": ["x"]}},  # unknown route
        {"text": "a", "kill_switch": {"kind": "product", "target": "999N001V02"}},  # irreversible
    ],
)
def test_malformed_turns_are_refused(turn: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Turn.model_validate(turn)


def test_scripted_analysis_is_completed_and_failures_pass_through() -> None:
    turn = Turn.model_validate(
        {"text": "a", "script": {
            "nlu-extract": [{"intents": ["META_HUMAN"]}],
            "guard-input": [{"content": "", "status": 503}],
        }}
    )  # fmt: skip
    (nlu,) = turn.replies("nlu-extract")
    assert nlu == {"content": {"intents": ["META_HUMAN"], "slots": [], "side_query": None,
                               "language": "en"}}  # fmt: skip
    assert turn.replies("guard-input") == [{"content": "", "status": 503}]
    assert copy.deepcopy(turn).withdraws is False
