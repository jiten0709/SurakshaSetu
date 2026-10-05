"""The golden conversation harness (Step 17): the conversation format, the transcript a run
produces, and the global assertions every conversation must pass (TDD §3.2, I1-I8, plus the audit
chain, PII and number checks).

A conversation is YAML under content/golden/conversations/<suite>/. test_conversations.py plays each
one against the running stack: the orchestrator in-process with its real lifespan, over the dev
database, valkey, domain-services and OmniRoute in front of the stubs. It collects a Transcript and
runs `check` on it.

The assertions are pure functions of the Transcript, so tests/unit/test_golden_assertions.py proves
each one catches a planted violation, including those no conversation can trip yet.
"""

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Self, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from surakshasetu.audit.events import EventType
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.fsm.states import FsmState
from surakshasetu.rails import redact

CONVERSATIONS = Path(__file__).resolve().parents[3] / "content" / "golden" / "conversations"
# Step 20: shared opening turns (a list of turns), named by a conversation's `prelude`.
PRELUDES = CONVERSATIONS.parent / "preludes"
ROUTES = (
    "guard-input",
    "nlu-extract",
    "gen-converse",
    "gen-recommend",
    "verify-claims",
    "summarise",
)
# nlu-extract scripts name only what they need; the rest of TurnAnalysis is filled in.
ANALYSIS = {"intents": [], "slots": [], "side_query": None, "language": "en"}
UIN = re.compile(r"\b999[NA]\d{3}V\d{2}\b", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
# Part ids whose text is approved, fixed content rather than model output: bundle templates,
# disclosure sets, and (Step 18) the registry's single disclosures and the consent notice.
FIXED_PARTS = ("template:", "disclosures:", "registry:", "notice:")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GivenConsent(_Strict):
    """A consent record captured through the real Consent Service before the first turn, as S0
    leaves a session. Since Step 18 a conversation can reach it through S0's own turns instead."""

    purposes: list[Literal["P1", "P2", "P3"]] = ["P1"]
    adult: bool = True
    captured_days_ago: int = Field(default=0, ge=0)


class Given(_Strict):
    """Where the session starts. Set as superuser on conv.session before the first turn; the turns
    themselves stay black-box."""

    consent: GivenConsent | None = None
    state: FsmState | None = None
    paused_from: FsmState | None = None  # with state PAUSE: the frame a resume returns to
    pins: dict[str, str] = {}  # e.g. a session created on a bundle since retired

    @model_validator(mode="after")
    def _pause_has_a_frame(self) -> Self:
        if (self.state is FsmState.PAUSE) != (self.paused_from is not None):
            raise ValueError("paused_from goes with state PAUSE, and only with it")
        return self


class KillSwitch(_Strict):
    kind: Literal["prompt_bundle", "route"]  # never a product: the dev catalog's are irreversible
    target: str
    reason_code: str = "GOLDEN_KILL_SWITCH"


class Action(_Strict):
    type: str
    payload: dict[str, Any] = {}


class ConsentExpect(_Strict):
    """The turn's CONSENT_CAPTURED header (Step 18)."""

    method: Literal["structured_action", "parsed_affirmation"] | None = None
    purposes: list[Literal["P1", "P2", "P3"]] | None = None
    language: Literal["en-IN", "hi-IN"] | None = None


class EngineExpect(_Strict):
    """An ENGINE_DECISION in the turn (Step 19): its service, and the result's outcome, flags and
    reason codes where given (from the decrypted payload)."""

    service: Literal["eligibility", "quote", "suitability", "ranking"]
    outcome: str | None = None
    flags: list[str] | None = None
    reason_codes: list[str] | None = None
    result: dict[str, Any] = {}  # Step 20: these members of the result (affordability, ...)


class Expect(_Strict):
    status: int = 200
    state: FsmState | None = None
    templates: list[str] | None = None  # the template ids of message.parts, in order
    events: list[EventType] = []  # each must occur in this turn's audit events
    handoff: str | None = None  # the HANDOFF reason code in this turn
    erased: Literal["scheduled", "destroyed"] | None = None  # live rows gone; the key's fate
    citations: bool | None = None
    abstained: bool | None = None
    disclosures: list[str] | None = None  # UINs with a disclosure set in the message
    hydrated: bool = False  # this turn rebuilt the session from conv (the checkpoint lagged)
    # Step 18
    consent: ConsentExpect | None = None  # a CONSENT_CAPTURED in this turn, with these fields
    absent: list[EventType] = []  # none of these events in this turn
    counters: dict[str, int] | None = None  # these conv.session counters after the turn
    slot_rows: int | None = None  # conv.slot_value rows after the turn
    form: bool | None = None  # the message carries the consent form
    # Step 19
    engine: EngineExpect | None = None
    slots: dict[str, Literal["proposed", "confirmed", "corrected", "declined"]] | None = None
    parts: list[str] | None = None  # every part id, in order (templates, registry, generated)
    contains: list[str] = []  # each appears in the released text
    lacks: list[str] = []  # none appears in the released text
    row: str | None = None  # the turn's STATE_TRANSITION trigger (the fsm row id)
    reason: str | None = None  # ... and its reason code


class Turn(_Strict):
    text: str | None = None
    action: Action | None = None
    delete: bool = False  # DELETE /v1/sessions/{id}
    retry: bool = False  # resend the previous request with its Idempotency-Key
    concurrent: bool = False  # send `text` twice at once with two keys: one 200, one 409
    checkpoint_lost: bool = False  # the process dies after the commit, before the checkpoint
    identity: bool = False  # an "am I talking to a person?" question (I6)
    kill_switch: KillSwitch | None = None  # set by ops before this turn is sent
    # A newer consent notice for the session's language takes effect before this turn (Step 18);
    # the harness removes it afterwards.
    notice_bump: bool = False
    script: dict[str, list[str | dict[str, Any]]] = {}  # route -> stub replies for this turn
    expect: Expect = Expect()

    @model_validator(mode="after")
    def _one_request(self) -> Self:
        kinds = [self.text is not None, self.action is not None, self.delete, self.retry]
        if sum(kinds) != 1:
            raise ValueError("a turn is exactly one of text, action, delete and retry")
        if self.concurrent and self.text is None:
            raise ValueError("concurrent sends text")
        if unknown := set(self.script) - set(ROUTES):
            raise ValueError(f"unknown routes in script: {sorted(unknown)}")
        return self

    @property
    def withdraws(self) -> bool:
        """A withdrawal: by free text (the scripted analysis says so), the ERASE action, DELETE."""
        intents = [
            i for r in self.script.get("nlu-extract", []) if isinstance(r, dict)
            for i in r.get("intents", [])
        ]  # fmt: skip
        erase = self.action is not None and self.action.type == "ERASE"
        return "META_WITHDRAW" in intents or erase or self.delete

    def replies(self, route: str) -> list[str | dict[str, Any]]:
        """The stub's /__script responses. A dict in the YAML is the model's JSON reply (an
        nlu-extract one gets the rest of TurnAnalysis filled in), unless it is already the stub's
        {content, status, headers} shape, which scripts a failure."""
        out: list[str | dict[str, Any]] = []
        for reply in self.script.get(route, []):
            if isinstance(reply, dict) and not set(reply) <= {"content", "status", "headers"}:
                reply = {"content": ANALYSIS | reply if route == "nlu-extract" else reply}
            out.append(reply)
        return out


class Conversation(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    description: str
    locale: Literal["en-IN", "hi-IN"] = "en-IN"
    channel: Literal["web", "app"] = "web"
    given: Given = Given()
    # Step 20: the opening turns of preludes/<name>.yaml, played before `turns` (they count in
    # the turn indices of failures, and every global assertion covers them).
    prelude: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{2,63}$")
    turns: list[Turn] = Field(min_length=1)


def with_prelude(raw: dict[str, Any], preludes: Path = PRELUDES) -> dict[str, Any]:
    """A conversation's YAML with its prelude's turns put first."""
    if not raw.get("prelude"):
        return raw
    opening = yaml.safe_load((preludes / f"{raw['prelude']}.yaml").read_text("utf-8"))
    if not isinstance(opening, list) or not opening:
        raise ValueError(f"prelude {raw['prelude']} is not a list of turns")
    return {**raw, "turns": [*opening, *raw.get("turns", [])]}


def load_conversations(root: Path = CONVERSATIONS, preludes: Path = PRELUDES) -> list[Conversation]:
    """Every <suite>/*.yaml, sorted. Dot-directories (tool caches) are skipped; ids are unique."""
    paths = sorted(
        p for p in root.glob("*/*.yaml") if not any(part.startswith(".") for part in p.parts)
    )
    found = [
        Conversation.model_validate(with_prelude(yaml.safe_load(p.read_text("utf-8")), preludes))
        for p in paths
    ]
    ids = [c.id for c in found]
    if len(ids) != len(set(ids)):
        raise ValueError("conversation ids must be unique")
    return found


# --- what a run produces --------------------------------------------------------------------------
@dataclass(frozen=True)
class Event:
    """An audit event as the harness reads it: payload None once the subject key is destroyed."""

    seq: int
    event_type: str
    header: dict[str, Any]
    pins: dict[str, Any]
    payload: dict[str, Any] | None = None
    fsm_state: str = ""


@dataclass
class Sent:
    """One request the conversation made and what came back."""

    turn: int  # index into Conversation.turns
    status: int
    body: bytes
    customer_text: str | None = None
    replay_of: int | None = None  # the earlier exchange whose key was resent
    counters: dict[str, int] | None = None  # conv.session.counters after it (None once erased)
    slot_rows: int = 0  # conv.slot_value rows after it
    slots: dict[str, str] = field(
        default_factory=dict
    )  # the newest row's status per slot (Step 19)

    @property
    def released(self) -> dict[str, Any] | None:
        """The turn body of a new 200 (a replay is not a new release)."""
        if self.status != 200 or self.replay_of is not None:
            return None
        body: dict[str, Any] = json.loads(self.body)
        return body


@dataclass
class Transcript:
    conversation: Conversation
    active_bundle: str
    consent_valid_from_start: bool
    initial_pins: dict[str, Any]
    sent: list[Sent] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    switched_bundles: set[str] = field(default_factory=set)  # prompt-bundle KILL_SWITCHes
    chain_ok: bool = False
    chain_checked: int = 0
    slots_without_consent: int = 0  # slot rows seen while no valid P1 existed
    redacted: list[str] = field(default_factory=list)  # every conv.turn.redacted seen
    briefings: list[dict[str, Any]] = field(default_factory=list)
    logs: str = ""
    erased: bool | None = None  # after a withdrawal: live rows, checkpoint gone, key handled
    products: dict[str, str] = field(default_factory=dict)  # base product UIN -> catalog name
    registry: dict[str, str] = field(default_factory=dict)  # uin -> registry set_sha256
    evidence: dict[str, str] = field(default_factory=dict)  # chunk_id -> text
    # Step 18: the Consent Service's notices (version -> (body, body_sha256)) for the parts and
    # forms released, and the registry's single-disclosure bodies (every language) that registry:
    # parts may carry: DISC-GLOBAL-AI-06 (Step 18) and DISC-GLOBAL-QUOTE-02 (Step 19).
    notices: dict[str, tuple[str, str]] = field(default_factory=dict)
    registry_bodies: set[str] = field(default_factory=set)

    def turn_events(self, released: dict[str, Any]) -> list[Event]:
        """One committed turn's events: from its TURN_INPUT to the next TURN_INPUT."""
        start = next(
            (i for i, e in enumerate(self.events)
             if e.event_type == "TURN_INPUT" and e.header.get("turn_id") == released["turn_id"]),
            None,
        )  # fmt: skip
        if start is None:
            return []
        end = next(
            (i for i in range(start + 1, len(self.events))
             if self.events[i].event_type == "TURN_INPUT"),
            len(self.events),
        )  # fmt: skip
        return self.events[start:end]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def part_ids(released: dict[str, Any]) -> list[str]:
    return [p["id"] for p in released["message"]["parts"]]


def pii_values(texts: Iterable[str]) -> set[str]:
    """The raw PII the customer typed, as the redaction rail finds it."""
    return {text[start:end] for text in texts for _, start, end in redact.entities(text)}


# --- the global assertions ------------------------------------------------------------------------
def check(t: Transcript) -> list[str]:
    """Every global assertion; an empty list means the conversation passed."""
    return [
        *check_chain(t),
        *check_i1(t),
        *check_i2(t),
        *check_i3(t),
        *check_i4(t),
        *check_i5(t),
        *check_i6(t),
        *check_i7(t),
        *check_i8(t),
        *check_pii(t),
        *check_numbers(t),
        *check_s0_no_generation(t),
        *check_consent_prompt(t),
    ]


def check_chain(t: Transcript) -> list[str]:
    if not t.chain_ok or t.chain_checked != len(t.events):
        return [f"chain: verify ok={t.chain_ok}, checked {t.chain_checked} of {len(t.events)}"]
    return []


def check_i1(t: Transcript) -> list[str]:
    """No slot row and no decision service before a valid P1 consent record exists."""
    valid_from = 0 if t.consent_valid_from_start else None
    failures = []
    for e in t.events:
        captured = e.event_type == "CONSENT_CAPTURED" and "P1" in e.header.get("purposes", [])
        if valid_from is None and captured:
            valid_from = e.seq
        if e.event_type == "ENGINE_DECISION" and (valid_from is None or e.seq < valid_from):
            failures.append(f"I1: ENGINE_DECISION at seq {e.seq} before a valid P1")
    if t.slots_without_consent:
        failures.append(f"I1: {t.slots_without_consent} slot rows without a valid P1")
    return failures


def check_i2(t: Transcript) -> list[str]:
    """Every entry to S3 has a current suitability record (the FSM's I2) and a decision for it.
    Since Step 20 that decision is bound to the needs it was asked for: the JCS hash of the
    request's needs (minus slots_sha256), recomputed here, equals the header's inputs_sha256, the
    request's slots_sha256 and the result's inputs_sha256, and the outcome was FIT. (Once a subject
    key is destroyed the payload is unreadable, and only the header-level check stands.)"""
    failures: list[str] = []
    decision: Event | None = None
    for e in t.events:
        if e.event_type == "ENGINE_DECISION" and e.header.get("service") == "suitability":
            decision = e
        entered = (
            e.event_type == "STATE_TRANSITION"
            and e.header["to_state"] == "S3"
            and e.header["from_state"] != "S3"
        )
        if not entered:
            continue
        if not (e.header["invariants"].get("I2") and decision is not None):
            failures.append(f"I2: S3 entered at seq {e.seq} without a current suitability")
        elif decision.payload is not None and (problem := _bound(decision)):
            failures.append(f"I2: S3 entered at seq {e.seq} on a decision {problem}")
    return failures


def _bound(decision: Event) -> str | None:
    payload = cast(dict[str, Any], decision.payload)
    needs = dict(payload.get("request", {}).get("needs", {}))
    result = payload.get("result", {})
    sent = needs.pop("slots_sha256", None)
    digest = sha256_hex(needs)
    hashes = {digest, decision.header.get("inputs_sha256"), sent, result.get("inputs_sha256")}
    if len(hashes) != 1:
        return "whose hashes differ from the needs it was asked for"
    if result.get("outcome") != "FIT":
        return f"with outcome {result.get('outcome')}"
    return None


def named(text: str, products: dict[str, str]) -> set[str]:
    """The products a text names, as UINs: written as a UIN, or by catalog name."""
    folded = text.casefold()
    return {u.upper() for u in UIN.findall(text)} | {
        uin for uin, name in products.items() if name.casefold() in folded
    }


def check_i3(t: Transcript) -> list[str]:
    """No UIN or product name released before S3, unless the customer named that product. Named
    either way, by name or by UIN, the product is the customer's (decided 2026-10-04, Step 19): the
    Quote-Only card shows the plan's name and UIN."""
    failures, seen_s3, said = [], False, ""
    for s in t.sent:
        said += " " + (s.customer_text or "")
        released = s.released
        if released is None:
            continue
        if released["state"] == "S3":
            seen_s3 = True
        if seen_s3:
            continue
        shown = named(released["message"]["text"], t.products)
        if leaked := sorted(shown - named(said, t.products)):
            failures.append(f"I3: turn {s.turn} released {leaked} before S3")
    return failures


def check_i4(t: Transcript) -> list[str]:
    """Every S3 release carries disclosure sets whose hashes are the registry's."""
    failures = []
    for s in t.sent:
        released = s.released
        if released is None or released["state"] != "S3":
            continue
        shown = released["message"]["disclosures"]
        for d in shown:
            if t.registry.get(d["uin"]) != d["set_sha256"]:
                failures.append(f"I4: turn {s.turn} {d['uin']} set hash is not the registry's")
        release = next(
            (e for e in t.turn_events(released) if e.event_type == "RESPONSE_RELEASED"), None
        )
        audited = release.header["disclosure_set_sha256s"] if release else None
        if audited != sorted(d["set_sha256"] for d in shown):
            failures.append(f"I4: turn {s.turn} RESPONSE_RELEASED disclosure hashes differ")
    return failures


def check_i5(t: Transcript) -> list[str]:
    """A withdrawal is honoured in the turn it is made: audited, erased, and never reported as
    withdrawn while the Consent Service has not recorded it."""
    failures, withdrew = [], False
    turns = t.conversation.turns
    for s in t.sent:
        released = s.released
        if released is None:
            continue
        events = t.turn_events(released)
        requests = [e for e in events if e.event_type == "ERASURE_REQUEST"]
        asked = turns[s.turn].withdraws
        withdrew |= asked
        if asked and not requests:
            failures.append(f"I5: turn {s.turn} withdrew but no ERASURE_REQUEST in that turn")
        for request in requests:
            if not asked and request.header["reason_code"] != "MINOR":
                failures.append(f"I5: turn {s.turn} erased without a request or a minor signal")
            ids = part_ids(released)
            pending = request.header["consent_withdrawal"] == "pending"
            claimed = "template:erasure_pending" if not pending else "template:erasure_done"
            if claimed in ids:  # "withdrawn" while pending, or "pending" once recorded
                failures.append(f"I5: turn {s.turn} reply does not match the withdrawal recorded")
    if withdrew and not t.erased:
        failures.append("I5: the withdrawal left live rows, a checkpoint or an unhandled key")
    return failures


def check_i6(t: Transcript) -> list[str]:
    """A question about talking to a person is answered first by the AI re-disclosure template,
    after the crisis script when that leads, and with fixed text only: nothing generated."""
    failures = []
    for s in t.sent:
        released = s.released
        if released is None or not t.conversation.turns[s.turn].identity:
            continue
        ids = [i for i in part_ids(released) if i != "template:safety"]
        if ids[:1] != ["template:ai_redisclosure"] or not all(
            i.startswith(FIXED_PARTS) for i in ids
        ):
            failures.append(f"I6: turn {s.turn} answered an identity question with {ids}")
    return failures


def check_i7(t: Transcript) -> list[str]:
    """Pins never change, except a prompt bundle re-pinned to the active one by its kill switch,
    and (decided 2026-10-04) the consent notice re-pinned by a CONSENT_CAPTURED naming the new one,
    the event where it changes."""
    failures, before = [], t.initial_pins
    for e in t.events:
        if e.pins == before:
            continue
        changed = {k for k in before.keys() | e.pins.keys() if before.get(k) != e.pins.get(k)}
        old, new = before.get("prompt_bundle"), e.pins.get("prompt_bundle")
        repinned = old in t.switched_bundles and new == t.active_bundle
        reconsented = e.event_type == "CONSENT_CAPTURED" and e.header.get(
            "notice_version"
        ) == e.pins.get("consent_notice")
        allowed = {"prompt_bundle"} if repinned else set()
        allowed |= {"consent_notice"} if reconsented else set()
        if not changed <= allowed:
            failures.append(f"I7: pins {sorted(changed)} changed at seq {e.seq}")
        before = e.pins
    return failures


def check_i8(t: Transcript) -> list[str]:
    """Every delivered message is a committed release: its hash and body are the audit record's,
    and a replay is byte-identical."""
    failures, delivered = [], 0
    for s in t.sent:
        if s.replay_of is not None and s.status == 200:
            if s.body != t.sent[s.replay_of].body:
                failures.append(f"I8: turn {s.turn} replay differs from the original")
            continue
        released = s.released
        if released is None:
            continue
        delivered += 1
        release = next(
            (e for e in t.turn_events(released) if e.event_type == "RESPONSE_RELEASED"), None
        )
        if release is None:
            failures.append(f"I8: turn {s.turn} delivered without a committed RESPONSE_RELEASED")
            continue
        if release.header["rendered_sha256"] != sha256_text(released["message"]["text"]):
            failures.append(f"I8: turn {s.turn} delivered text is not the committed hash")
        if release.payload is not None and release.payload.get("response") != released:
            failures.append(f"I8: turn {s.turn} delivered body is not the committed payload")
    committed = sum(e.event_type == "RESPONSE_RELEASED" for e in t.events)
    if committed != delivered:
        failures.append(f"I8: {committed} releases committed, {delivered} delivered")
    return failures


def check_pii(t: Transcript) -> list[str]:
    """The customer's raw PII is in no header, redacted text, log line or advisor briefing."""
    sentinels = pii_values(s.customer_text for s in t.sent if s.customer_text)
    places = {
        "audit headers": json.dumps([e.header for e in t.events], ensure_ascii=False),
        "redacted text": "\n".join(t.redacted),
        "logs": t.logs,
        "briefings": json.dumps(t.briefings, ensure_ascii=False),
    }
    return [
        f"PII: a value the customer typed appears in {place}"
        for value in sorted(sentinels)
        for place, text in places.items()
        if value in text
    ]


def check_numbers(t: Transcript) -> list[str]:
    """Numbers in model-written parts come from engine values or the cited evidence only."""
    engine = " ".join(
        json.dumps(e.payload) for e in t.events if e.event_type == "ENGINE_DECISION" and e.payload
    )
    allowed = set(NUMBER.findall(engine)) | set(NUMBER.findall(" ".join(t.evidence.values())))
    failures = []
    for s in t.sent:
        released = s.released
        if released is None:
            continue
        for part in released["message"]["parts"]:
            if part["id"].startswith(FIXED_PARTS):
                continue
            if stray := sorted(set(NUMBER.findall(part["text"])) - allowed):
                failures.append(f"numbers: turn {s.turn} part {part['id']} released {stray}")
    return failures


def check_s0_no_generation(t: Transcript) -> list[str]:
    """S0 generates no text (TDD §3.5): no generation route is called while the session is in S0."""
    return [
        f"S0: MODEL_CALL to {e.header.get('route')} at seq {e.seq}"
        for e in t.events
        if e.event_type == "MODEL_CALL"
        and e.fsm_state == "S0"
        and str(e.header.get("route", "")).startswith("gen-")
    ]


def check_consent_prompt(t: Transcript) -> list[str]:
    """The notice and the registry's single disclosures (the AI disclosure; Step 19, the indicative
    quote's) are released verbatim, and every consent form names a notice the Consent Service
    holds, with its hash."""
    failures = []
    for s in t.sent:
        released = s.released
        if released is None:
            continue
        for part in released["message"]["parts"]:
            pid, text = part["id"], part["text"]
            if pid.startswith("notice:") and t.notices.get(pid[7:], ("",))[0] != text:
                failures.append(f"consent: turn {s.turn} {pid} is not the notice verbatim")
            if pid.startswith("registry:") and text not in t.registry_bodies:
                failures.append(f"consent: turn {s.turn} {pid} is not the registry's body")
        form = released["message"].get("form")
        if form is not None:
            held = t.notices.get(form["notice_version"])
            if held is None or held[1] != form["notice_sha256"]:
                failures.append(f"consent: turn {s.turn} form names a notice not held")
    return failures
