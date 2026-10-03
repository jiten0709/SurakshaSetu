"""The golden harness's own checks, offline: every conversation file validates, and each global
assertion catches a planted violation. Several can't be tripped by a Step 17 conversation (I2,
I4, I6, numbers), and this file is what proves they aren't vacuous."""

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

PINS = {"prompt_bundle": "pb-2026.10.1", "rules": "2026.09.1"}


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
        active_bundle="pb-2026.10.1",
        consent_valid_from_start=False,
        initial_pins=PINS,
        sent=[Sent(0, 200, raw, customer_text="hello")],
        events=events(raw),
        chain_ok=True,
        chain_checked=3,
        product_names=["Suraksha Term Shield"],
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


def test_i6_an_identity_question_gets_the_re_disclosure_only() -> None:
    t = transcript({"text": "are you a human?", "identity": True})
    assert any(f.startswith("I6") for f in check(t))
    raw = body(parts=[{"id": "template:ai_redisclosure", "text": "Hi"}])
    t.sent, t.events = [Sent(0, 200, raw)], events(raw)
    assert not any(f.startswith("I6") for f in check(t))


def test_i7_pins_change_only_by_a_bundle_kill_switch() -> None:
    raw = body()
    moved = transcript(events=events(raw, pins=PINS | {"rules": "2026.10.1"}))
    assert any(f.startswith("I7: pins ['rules']") for f in check(moved))
    repinned = transcript(initial_pins=PINS | {"prompt_bundle": "pb-2026.09.1"})
    assert any(f.startswith("I7") for f in check(repinned))  # no kill switch recorded
    repinned.switched_bundles = {"pb-2026.09.1"}
    assert check(repinned) == []


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
