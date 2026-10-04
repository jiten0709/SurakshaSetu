"""The S1 node: State-1, rapport and eligibility screening (TDD §3.6; Step 19).

S1 asks only the attributes the pinned rules reference (GET /v1/eligibility/required-attributes,
in the DMN's order; `asked_if` conditions such as "proposer.is_life_assured = false"), one per
turn: the bundle's question and its approved one-line reason, verbatim. When that is the whole
reply, compose adds one friendly sentence from gen-converse with the S1 L1 (Turn.phrase); on any
gateway or rail failure the template goes out alone.

Slots fill only from what the customer said: nlu-extract's candidates (each with its evidence span)
and, for the question just asked, the deterministic normalisers over the whole message ("34",
"३४", "born in '91", a pincode, yes/no/prefer not to say), or a quick reply checked against the
open question. Confidence of 0.8 and above proposes the value, 0.6-0.8 proposes it and reads it
back at once, and below 0.6 the value is dropped (input_node counts the low-confidence streak; CC2
escalates at the limit). Every value is appended to conv.slot_value, encrypted, with the session's
consent_id (I1): proposed, corrected (a later value for the same slot), declined, and confirmed by
the read-back. An age under 18 is the minor signal before anything is written (V2).

Once every asked attribute has a value, the read-back ("To confirm: 34, Pune, ...") lists them from
the slots. Confirmed, the Eligibility Service decides (ENGINE_DECISION), and decide applies S1's
rows: 0 DATA_ERASURE_EXIT, 1 HUMAN_ESCALATION (NRI/OCI/PIO, age band, complex proposer), 1b RE_ASK
after the one re-ask, 2 NOT_ELIGIBLE -> Exit, 3 eligible + express path -> Quote-Only, 4 S2. The
model never decides eligibility, and no prompt text speaks about it.

Edge cases (TDD §3.6, §3.9): a serious illness disclosed (the health question answered yes, or a
condition named) gets the empathetic acknowledgement and an advisor offer; a question about how a
condition affects acceptance gets the underwriting note (a template until Step 22's side-query
subgraph cites the corpus); "don't tell them I smoke" gets the non-disclosure note; an early price
request offers the Quote-Only express path; an answer the system cannot use gets the slot's format
hint, and the advisor offer after SS_INVALID_INPUT_LIMIT tries; off-topic gets a one-line redirect;
the domain tier down gets a retry.

`Screen` is shared with Quote-Only (graph/states/quote_only.py), and `correction` is the V4 hook the
S2 and S3 nodes call: a changed eligibility fact writes a corrected row and re-runs S1's rows (G2).
"""

import dataclasses
import json
import logging
import re
from datetime import UTC, datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime
from pydantic import ValidationError

from surakshasetu.analysis.models import Intent
from surakshasetu.analysis.normalisers import (
    devanagari_digits_to_ascii,
    parse_age,
    parse_money,
    parse_pincode,
    parse_yes_no,
)
from surakshasetu.audit.events import EngineDecisionHeader, EventType
from surakshasetu.compose.bundle import SlotTemplate, mentions, phrase
from surakshasetu.domain.client import DomainError
from surakshasetu.domain.models import (
    EligibilityRequest,
    EligibilityResult,
    Occupation,
    Pins,
    Proposer,
    RequiredAttribute,
)
from surakshasetu.graph.handlers import append, bundle, quick_reply, scripts, session
from surakshasetu.graph.state import EligibilityPayload, GraphState, SessionState, SlotRow
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
HEALTH = "RL-S1-HEALTH"  # the one (DUMMY) screening question: health_flags is keyed by it
Kind = Literal[
    "age", "choice", "relationship", "pincode", "yn", "ynd", "occupation", "money", "years"
]
# How each slot's value is understood. The eligibility slots are the rules' attribute names;
# sum_assured_inr and term_years are what a customer may state in Quote-Only.
KINDS: dict[str, Kind] = {
    "age_years": "age",
    "gender": "choice",
    "residency": "choice",
    "pincode": "pincode",
    "tobacco_12m": "ynd",
    "occupation_code": "occupation",
    "health_flags": "ynd",
    "proposer.is_life_assured": "yn",
    "proposer.relationship": "relationship",
    "proposer.la_age": "age",
    "proposer.business_cover": "yn",
    "sum_assured_inr": "money",
    "term_years": "years",
}
ELIGIBILITY = frozenset(KINDS) - {"sum_assured_inr", "term_years"}
CHOICES = {
    "gender": ("male", "female", "transgender"),
    "residency": ("resident", "nri", "oci_pio"),
    "proposer.relationship": ("spouse", "child", "parent", "other"),
}
# Words closed answers often arrive as (nlu-extract is told the slot names and usually
# canonicalises; quick replies always do).
SYNONYMS = {
    "resident_indian": "resident",
    "non_resident": "nri",
    "non_resident_indian": "nri",
    "oci": "oci_pio",
    "pio": "oci_pio",
    "wife": "spouse",
    "husband": "spouse",
    "son": "child",
    "daughter": "child",
    "mother": "parent",
    "father": "parent",
}
# Slot names a model may use for ours.
ALIASES = {
    "age": "age_years",
    "tobacco": "tobacco_12m",
    "smoker_status": "tobacco_12m",
    "occupation": "occupation_code",
    "is_proposer_life_assured": "proposer.is_life_assured",
    "relationship": "proposer.relationship",
    "la_age": "proposer.la_age",
    "business_cover": "proposer.business_cover",
    "residency_status": "residency",
    "cover": "sum_assured_inr",
    "term": "term_years",
}
_YEARS = re.compile(r"\b(\d{1,2})\s*(?:years?|yrs?|saal|साल)?\b", re.I)
_DIGITS = re.compile(r"\s*(\d{5,10})\s*")
QUESTIONS = frozenset({Intent.SIDE_QUERY, Intent.GENERAL_FAQ})


@dataclasses.dataclass(frozen=True)
class Answer:
    value: Any
    declined: bool = False
    derived: bool = False  # worked out, not said (an age from a birth year): read back at once


def valid(current: SessionState) -> bool:
    return current.consent is not None and current.consent.valid_p1


# --- understanding one value ----------------------------------------------------------------------
def _words(text: str) -> list[str]:
    return re.findall(r"[a-z_]+", text.casefold().replace("-", "_"))


def _choice(slot: str, text: str) -> str | None:
    words = _words(text)
    for word in ("_".join(words), *words):
        word = SYNONYMS.get(word, word)
        if word in CHOICES[slot]:
            return word
    return None


async def understand(
    turn: Any, slot: str, raw: Any, evidence: str
) -> Answer | list[Occupation] | None:
    """The slot's value from what the customer said, or None when it cannot be used. Several
    occupations matching is a list for the customer to choose from."""
    kind = KINDS[slot]
    said = raw if isinstance(raw, str) else evidence
    text = devanagari_digits_to_ascii(said).strip()
    if kind == "age":
        if isinstance(raw, int) and not isinstance(raw, bool):
            return Answer(raw) if 0 <= raw <= 120 else None
        year = datetime.now(IST).year
        parsed = parse_age(text, current_year=year) or parse_age(evidence, current_year=year)
        if parsed is not None:
            if parsed.value is None or not 0 <= parsed.value <= 120:
                return None  # two plausible ages for a two-digit year: asked again
            return Answer(parsed.value, derived=parsed.needs_confirmation)
        numbers = re.findall(r"\d+", text)  # one number in a short answer: "34", "मैं 34 का हूँ"
        if len(numbers) == 1 and len(text) <= 40 and int(numbers[0]) <= 120:
            return Answer(int(numbers[0]))
        return None
    if kind in ("yn", "ynd"):
        word = "yes" if raw is True else "no" if raw is False else parse_yes_no(phrase(text))
        if word is None or (word == "declined" and kind == "yn"):
            return None
        flag = None if word == "declined" else word == "yes"
        return Answer({HEALTH: flag} if slot == "health_flags" else flag, word == "declined")
    if kind == "choice":
        chosen = _choice(slot, text)
        return Answer(chosen) if chosen else None
    if kind == "relationship":
        chosen = _choice(slot, text)
        free = " ".join(w for w in _words(text) if w not in ("my", "the", "is", "for"))[:40]
        return Answer(chosen or free) if chosen or free else None
    if kind == "pincode":
        pincode = parse_pincode(text) or parse_pincode(evidence)
        if pincode is None:
            return None
        try:
            await turn.domain.get_pincode(pincode)  # validated against the master (TDD §3.6)
        except DomainError as exc:
            if exc.code == "NOT_FOUND":
                return None
            raise
        return Answer(pincode)
    if kind == "occupation":
        if parse_yes_no(phrase(text)) == "declined":
            return Answer(None, declined=True)
        return await _occupation(turn, text)
    if kind == "money":
        if isinstance(raw, int) and not isinstance(raw, bool):
            return Answer(raw) if raw > 0 else None
        money = parse_money(text) or parse_money(evidence)
        if money is not None and money.annual_inr:
            return Answer(money.annual_inr)
        digits = _DIGITS.fullmatch(text.replace(",", ""))
        return Answer(int(digits.group(1))) if digits else None
    if isinstance(raw, int) and not isinstance(raw, bool):  # years
        return Answer(raw) if 0 < raw <= 85 else None
    years = _YEARS.search(text)
    return Answer(int(years.group(1))) if years and 0 < int(years.group(1)) <= 85 else None


async def _occupation(turn: Any, text: str) -> Answer | list[Occupation] | None:
    """A code from the occupation master (TDD §3.6), searched by what the customer said, then by
    its longer words. One match is the answer; several are offered as choices."""
    if not text or len(text) > 80:
        return None
    words = sorted({w for w in re.findall(r"\w{4,}", text)}, key=len, reverse=True)
    for q in [text, *words[:2]]:
        found: list[Occupation] = await turn.domain.search_occupations(q)
        if len(found) == 1:
            return Answer(found[0].code)
        if found:
            return found[:5]
    return None


def _applies(condition: str | None, known: dict[str, tuple[str, Any]]) -> bool:
    """An attribute's asked_if. The rules use one form only, "<slot> = <JSON literal>"; anything
    else is refused rather than guessed."""
    if condition is None:
        return True
    slot, sep, literal = condition.partition(" = ")
    if not sep:
        raise ValueError("unsupported asked_if condition")
    return slot in known and known[slot][1] == json.loads(literal)


# --- the screen shared by S1 and Quote-Only -------------------------------------------------------
@dataclasses.dataclass
class Screen:
    """One turn's view of the screening: the rules' attributes, what each holds (the newest
    conv.slot_value row, overlaid with this turn's), and the reply being built."""

    turn: Any
    session: SessionState
    prefix: Literal["s1", "qo"]  # prompt ids: <prefix>.ask:<slot>, .confirm:<slot>, .readback, ...
    attrs: list[RequiredAttribute]
    known: dict[str, tuple[str, Any]]
    extra: tuple[str, ...] = ()  # slots a customer may state beyond the rules' (Quote-Only)
    lead: list[tuple[str, str]] = dataclasses.field(default_factory=list)
    replies: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    filled: set[str] = dataclasses.field(default_factory=set)
    offered: set[str] = dataclasses.field(default_factory=set)  # slots nlu-extract proposed
    confirm: str | None = None  # a value to read back at once
    options: tuple[str, list[Occupation]] | None = None
    noted: bool = False  # the turn was something other than an answer (a note, FAQ, off-topic...)

    @classmethod
    async def load(
        cls,
        turn: Any,
        current: SessionState,
        prefix: Literal["s1", "qo"],
        *,
        only: tuple[str, ...] | None = None,
        extra: tuple[str, ...] = (),
    ) -> "Screen":
        attrs = await turn.domain.get_required_attributes(current.pins.rules)
        if unknown := sorted({a.attribute for a in attrs} - set(KINDS)):
            raise RuntimeError(f"the rules ask attributes S1 cannot read: {unknown}")
        if only is not None:
            attrs = [a for a in attrs if a.attribute in only]
        row = cast(SessionRow, turn.row)
        known = store.latest_slots(turn.conn, turn.keys, row.key_ref, turn.session_id)
        for slot_row in turn.slot_rows:
            known[slot_row.slot] = (slot_row.status, slot_row.value)
        return cls(turn, current, prefix, attrs, known, extra)

    # --- what is known ----------------------------------------------------------------------------
    @property
    def asked(self) -> list[RequiredAttribute]:
        return [a for a in self.attrs if _applies(a.asked_if, self.known)]

    def attr(self, slot: str) -> RequiredAttribute | None:
        return next((a for a in self.asked if a.attribute == slot), None)

    def missing(self) -> RequiredAttribute | None:
        return next((a for a in self.asked if a.attribute not in self.known), None)

    def confirmed(self) -> bool:
        return all(
            a.attribute in self.known and self.known[a.attribute][0] == "confirmed"
            for a in self.asked
        )

    def values(self) -> dict[str, Any]:
        return {slot: value for slot, (_, value) in self.known.items()}

    def write(self, slot: str, value: Any, status: str, confidence: float) -> None:
        self.turn.slot_rows.append(SlotRow(slot, value, confidence, cast(Any, status)))
        self.known[slot] = (status, value)
        if status != "confirmed" and slot in ELIGIBILITY:
            self.session.eligibility = None  # the engine's result no longer matches (V4)

    # --- taking answers ---------------------------------------------------------------------------
    async def take(self, slot: str, raw: Any, evidence: str, confidence: float) -> bool:
        """One evidenced value (or a verified quick reply, at confidence 1). True when it fills the
        slot."""
        settings = self.turn.settings
        rules = {a.attribute for a in self.attrs}
        if slot not in rules and slot not in self.extra:
            return False  # only what the rules ask: gender is never inferred
        if confidence < settings.confidence_readback_floor:
            return False
        answer = await understand(self.turn, slot, raw, evidence)
        if isinstance(answer, list):
            self.options = (slot, answer)
            return False
        if answer is None:
            return False
        if slot == "age_years" and answer.value < 18:  # V2: nothing is written
            self.turn.signals["minor"] = True
            logger.info("under 18 stated in %s: nothing recorded", self.session.fsm_state)
            return False
        prior = self.known.get(slot)
        self.filled.add(slot)
        if prior is not None and prior[1] == answer.value:
            return True  # said again: no new row
        status = "declined" if answer.declined else "corrected" if prior else "proposed"
        self.write(slot, answer.value, status, confidence)
        logger.info("slot %s %s", slot, status)
        if answer.derived or confidence < settings.confidence_accept:
            self.confirm = slot
        if slot == "health_flags" and answer.value[HEALTH] is True:
            self.disclosed()
        return True

    async def candidates(self) -> None:
        """This turn's nlu-extract candidates, each with its evidence span."""
        for candidate in self.turn.slots_pending:
            slot = ALIASES.get(candidate.slot, candidate.slot)
            if slot in KINDS:
                self.offered.add(slot)
                await self.take(
                    slot, candidate.value, candidate.evidence_span, candidate.confidence
                )

    async def answer(self) -> None:
        """The question just asked, read deterministically from the whole message: an answer turn,
        and only when nlu-extract proposed nothing for it (its confidence stands)."""
        turn = self.turn
        pending = self.pending()
        if (
            pending
            and pending not in self.offered | self.filled
            and not self.noted
            and turn.pipeline is not None
        ):
            text = turn.pipeline.stored_raw
            await self.take(pending, text, text, 1.0)

    def pending(self) -> str | None:
        """The slot of the open question. Not a read-back of one value: a "yes" there confirms it,
        and must never become the answer to a yes/no question."""
        prompt = self.session.last_prompt_id or ""
        return prompt.split(":", 1)[1] if prompt.startswith(f"{self.prefix}.ask:") else None

    async def slot_action(self, payload: dict[str, Any]) -> None:
        """A SLOT quick reply: only for the open question, and only with a value it offered."""
        slot, value = payload.get("slot"), payload.get("value")
        if slot is None or slot != self.pending() or not isinstance(value, str | int | bool):
            logger.warning("a SLOT action for another question: ignored")
            return
        await self.take(slot, value, str(value), 1.0)

    def notes(self) -> None:
        """What the turn is, beyond an answer (TDD §3.6 and §3.9's S1 column)."""
        turn = self.turn
        pipeline = turn.pipeline
        if pipeline is None:
            return
        texts, lexicon = scripts(turn), bundle(turn).screening_lexicon
        text = pipeline.stored_raw
        intents = set(pipeline.analysis.intents) if pipeline.analysis else set()
        question = "?" in text or bool(intents & QUESTIONS)
        if pipeline.blocked or turn.identity:
            self.noted = True  # slots discarded (injection) or compose answers (I6): nothing else
            return
        if mentions(lexicon.concealment, text):
            self.note("nondisclosure_note", texts.nondisclosure_note)
        if mentions(lexicon.health_terms, text):
            if question:
                self.note("underwriting_note", texts.underwriting_note)
            else:
                self.disclosed()
        if (
            Intent.EXPRESS_PATH in intents
            and self.prefix == "s1"
            and not self.session.counters.get("express_path")
        ):
            self.session.counters = {**self.session.counters, "express_path": 1}
            self.note("express_offer", texts.express_offer)
            logger.info("express path asked for in S1: Quote-Only once eligible")
        if Intent.OFF_TOPIC in intents:
            self.note("redirect", texts.redirect)
        # Any other question, unless it was an answer phrased as one: the state's caveat until
        # Step 22's side-query subgraph answers it.
        if question and not self.lead and not self.filled:
            self.note("side_query_caveat", texts.side_query_caveat[self.session.fsm_state.value])

    def note(self, part_id: str, text: str) -> None:
        if all(i != part_id for i, _ in self.lead):
            self.lead.append((part_id, text))
        self.noted = True

    def disclosed(self) -> None:
        """A serious illness disclosed: the acknowledgement and an advisor offer; no insurability
        statement, and no diagnosis stored (the engine flags MEDICAL_UW from the answer)."""
        sc = scripts(self.turn).screening
        self.note("medical_ack", scripts(self.turn).medical_ack)
        if not any(r["action"]["type"] == "HUMAN_REQUEST" for r in self.replies):
            self.replies.append(quick_reply(sc.advisor, "HUMAN_REQUEST", {}))

    def said(self) -> bool | None:
        return said(self.turn)

    # --- the turn -----------------------------------------------------------------------------
    async def respond(self) -> bool:
        """Take the turn and put the reply. True when every asked slot is confirmed: the caller
        decides (S1 evaluates, Quote-Only quotes)."""
        turn, current = self.turn, self.session
        action = turn.action or {}
        kind, payload = action.get("type"), action.get("payload") or {}
        prompt = current.last_prompt_id or ""
        if kind == "SLOT":
            await self.slot_action(payload)
        elif kind == "REASK":
            attr = self.attr(str(payload.get("slot")))
            if attr is not None:
                self.ask(attr)
                return False
        elif kind is None:
            await self.candidates()
            self.notes()
            await self.answer()
        if turn.signals.get("minor"):
            return False  # the erasure handler speaks
        if self.filled:
            self._count_invalid(reset=True)
        else:
            said = self.said()
            if prompt == f"{self.prefix}.readback" and said is not None:
                if said:
                    self.confirm_all()
                    return await self.next()
                self.fix()
                return False
            if prompt.startswith(f"{self.prefix}.confirm:") and said is False:
                attr = self.attr(prompt.split(":", 1)[1])
                if attr is not None:
                    self.ask(attr)
                    return False
            if prompt == f"{self.prefix}.advisor":
                if said:
                    turn.signals["human_request"] = True
                    return False
                self._count_invalid(reset=True)
            elif self._invalid(prompt, kind):
                return False
        return await self.next()

    def _invalid(self, prompt: str, kind: str | None) -> bool:
        """An answer to the open question the system could not use: the format hint, then the
        advisor offer at the limit (TDD §3.9). True when that is the reply."""
        if (
            kind is not None
            or not prompt.startswith(f"{self.prefix}.ask:")
            or self.noted
            or self.options
        ):
            return False
        attr = self.attr(prompt.split(":", 1)[1])
        if attr is None:
            return False
        texts = scripts(self.turn)
        if self._count_invalid() >= self.turn.settings.invalid_input_limit:
            self._count_invalid(reset=True)
            sc = texts.screening
            self.session.last_prompt_id = f"{self.prefix}.advisor"
            self.reply(
                [("advisor_offer", texts.advisor_offer)],
                [
                    quick_reply(sc.advisor, "HUMAN_REQUEST", {}),
                    quick_reply(sc.keep_going, "CONTINUE", {}),
                ],
            )
            logger.info("invalid input limit on %s: advisor offered", attr.attribute)
            return True
        hint = self.template(attr).hint or ""
        self.lead.append(("format_hint", texts.format_hint.format(hint=hint).rstrip()))
        self.ask(attr)
        return True

    def _count_invalid(self, *, reset: bool = False) -> int:
        counters = self.session.counters
        count = 0 if reset else counters.get("invalid_input", 0) + 1
        if count or counters.get("invalid_input"):
            self.session.counters = {**counters, "invalid_input": count}
        return count

    async def next(self) -> bool:
        """The next prompt after the lead parts. True when every asked slot is confirmed."""
        if self.options is not None:
            slot, found = self.options
            attr = self.attr(slot)
            if attr is not None:
                self.ask(
                    attr,
                    [quick_reply(o.label, "SLOT", {"slot": slot, "value": o.code}) for o in found],
                )
                return False
        if self.confirm is not None:
            await self.confirm_one(self.confirm)
            return False
        pending = self.pending()
        open_question = self.attr(pending) if pending and pending not in self.known else None
        if (attr := open_question or self.missing()) is not None:
            self.ask(attr)  # the open question again after a note, else the first missing
            return False
        if not self.confirmed():
            await self.readback()
            return False
        return True

    # --- prompts --------------------------------------------------------------------------------
    def template(self, attr: RequiredAttribute) -> SlotTemplate:
        return bundle(self.turn).templates[self.session.locale].slots[attr.reason_line_id]

    def reply(self, parts: list[tuple[str, str]], quick: list[dict[str, Any]]) -> None:
        self.turn.parts = [*self.lead, *parts]
        self.turn.quick_replies = [*quick, *self.replies]
        self.turn.form = None

    def ask(self, attr: RequiredAttribute, quick: list[dict[str, Any]] | None = None) -> None:
        slot = self.template(attr)
        self.session.pending_slot = attr.attribute
        self.session.last_prompt_id = f"{self.prefix}.ask:{attr.attribute}"
        part = (attr.reason_line_id, f"{slot.question} {slot.reason}")
        self.reply([part], self.choices(attr.attribute) if quick is None else quick)
        if self.prefix == "s1" and not self.lead:  # a plain question: one friendly sentence first
            self.turn.phrase = ("S1", attr.reason_line_id, tuple(sorted(self.known)))

    def choices(self, slot: str) -> list[dict[str, Any]]:
        labels = scripts(self.turn).screening.choices
        kind = KINDS[slot]
        key = {"yn": "yes_no", "ynd": "yes_no_declined"}.get(kind, slot)
        return [
            quick_reply(label, "SLOT", {"slot": slot, "value": value})
            for value, label in labels.get(key, {}).items()
        ]

    def _yes_no(self) -> list[dict[str, Any]]:
        sc = scripts(self.turn).screening
        return [
            quick_reply(sc.confirm, "CONFIRM", {"confirmed": True}),
            quick_reply(sc.fix, "CONFIRM", {"confirmed": False}),
        ]

    async def confirm_one(self, slot: str) -> None:
        """A value read back at once (0.6-0.8 confidence, or derived)."""
        texts = scripts(self.turn)
        self.session.pending_slot = slot
        self.session.last_prompt_id = f"{self.prefix}.confirm:{slot}"
        self.reply(
            [("readback", texts.readback.format(facts=await self.fact(slot)))], self._yes_no()
        )

    async def readback(self) -> None:
        """Every asked value, from the slots, before any decision (TDD §3.6)."""
        texts = scripts(self.turn)
        facts = [await self.fact(a.attribute) for a in self.asked]
        self.session.pending_slot = None
        self.session.last_prompt_id = f"{self.prefix}.readback"
        self.reply([("readback", texts.readback.format(facts=", ".join(facts)))], self._yes_no())

    def fix(self) -> None:
        """The read-back was wrong: which detail?"""
        texts = scripts(self.turn)
        names = texts.screening.names
        self.session.pending_slot = None
        self.session.last_prompt_id = f"{self.prefix}.fix"
        self.reply(
            [("readback_fix", texts.readback_fix)],
            [quick_reply(names[a.attribute], "REASK", {"slot": a.attribute}) for a in self.asked],
        )

    def confirm_all(self) -> None:
        for attr in self.asked:
            status, value = self.known[attr.attribute]
            if status != "confirmed":
                self.write(attr.attribute, value, "confirmed", 1.0)
        logger.info("read-back confirmed: %d slots", len(self.asked))

    async def fact(self, slot: str) -> str:
        """How one value reads back: the bundle's phrase, the pincode's district, the occupation's
        label. Numbers come from the slots only."""
        sc = scripts(self.turn).screening
        _, value = self.known[slot]
        phrasing = sc.facts[slot]
        if isinstance(phrasing, dict):
            if slot == "health_flags":
                value = value[HEALTH]
            key = "declined" if value is None else {True: "yes", False: "no"}.get(value, value)
            return phrasing[key]
        if value is None:  # a declined free answer (occupation)
            return f"{sc.names[slot]}: {sc.choices['yes_no_declined']['declined']}"
        if slot == "pincode":
            return phrasing.format(district=(await self.turn.domain.get_pincode(value)).district)
        if slot == "occupation_code":
            return phrasing.format(label=(await self.turn.domain.get_occupation(value)).label)
        return phrasing.format(value=value)


def said(turn: Any) -> bool | None:
    """A yes or no to the open prompt: the CONFIRM quick reply or a whole-message answer."""
    action = turn.action or {}
    if action.get("type") == "CONFIRM":
        confirmed = (action.get("payload") or {}).get("confirmed")
        return confirmed if isinstance(confirmed, bool) else None
    if turn.pipeline is None or turn.text is None:
        return None
    answer = parse_yes_no(phrase(turn.pipeline.stored_raw))
    return {"yes": True, "no": False}.get(answer or "")


def outage(turn: Any, exc: DomainError, part: tuple[str, str]) -> None:
    """The domain tier is down (TDD §3.9 "Dependency down"): the template and a retry; a 4xx is a
    contract problem and propagates."""
    if exc.status is not None and exc.status < 500:
        raise exc
    logger.warning("domain tier down in %s: %s; retry offered", session(turn).fsm_state, exc.code)
    turn.parts = [part]
    turn.quick_replies = [quick_reply(scripts(turn).screening.retry, "RETRY", {})]
    turn.form = None
    turn.phrase = None


# --- State-1 --------------------------------------------------------------------------------------
async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    current = session(turn)
    kind = (turn.action or {}).get("type")
    if not valid(current) or kind == "HUMAN_REQUEST":
        return  # G1 re-enters S0 (I1: no call, no row); CC2 hands over
    if turn.pipeline is not None and turn.pipeline.overlong:
        return  # compose asks to shorten; the question stands
    try:
        screen = await Screen.load(turn, current, "s1")
        if await screen.respond():
            await _decide(screen)
    except DomainError as exc:
        outage(turn, exc, ("screening_retry", scripts(turn).screening_retry))


async def enter(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """Another state's turn entered S1 (S0.4, QO.3b, G2, a resume): the next question, or the
    read-back when everything is known (a correction re-runs S1's rows, V4). It never evaluates:
    the transition for this turn is already decided."""
    turn = runtime.context
    current = session(turn)
    try:
        screen = await Screen.load(turn, current, "s1")
        if await screen.next():
            await screen.readback()
    except DomainError as exc:
        outage(turn, exc, ("screening_retry", scripts(turn).screening_retry))
    logger.info("S1 entered: %s", current.last_prompt_id)


async def _decide(screen: Screen) -> None:
    """Every asked slot confirmed: the engine decides, unless it already has for these values.
    After RE_ASK the occupation is asked again until it is answered: a new answer is read back
    first (it is a new value); the same answer again (declined again) goes straight back to the
    engine, which decides on the values already confirmed."""
    current = screen.session
    engine = current.eligibility.engine if current.eligibility else None
    if engine is None or (engine.outcome == "RE_ASK" and "occupation_code" in screen.filled):
        await _evaluate(screen)
    elif engine.outcome == "RE_ASK" and (attr := screen.attr("occupation_code")) is not None:
        screen.lead.append(("reask", scripts(screen.turn).reask))
        screen.ask(attr)


async def _evaluate(screen: Screen) -> None:
    turn, current = screen.turn, screen.session
    values = {a.attribute: screen.known[a.attribute][1] for a in screen.asked}
    request = EligibilityRequest(
        pins=Pins(rules=current.pins.rules),
        age_years=values["age_years"],
        gender=values.get("gender"),
        residency=values["residency"],
        pincode=values["pincode"],
        tobacco_12m=values.get("tobacco_12m"),
        occupation_code=values.get("occupation_code"),
        health_flags=values.get("health_flags") or {},
        proposer=Proposer(
            is_life_assured=values["proposer.is_life_assured"],
            relationship=values.get("proposer.relationship"),
            la_age=values.get("proposer.la_age"),
            business_cover=values.get("proposer.business_cover"),
        ),
    )
    result = await turn.domain.evaluate_eligibility(request)
    append(
        turn,
        EventType.ENGINE_DECISION,
        EngineDecisionHeader(
            service="eligibility",
            decision_id=str(result.decision_id),
            rules_version=result.rules_version,
            params_version=result.params_version,
            inputs_sha256=result.inputs_sha256,
            reason_codes=result.reason_codes,
        ),
        {"request": request.model_dump(mode="json"), "result": result.model_dump(mode="json")},
    )
    current.eligibility = _payload(request, result)
    turn.signals["eligibility"] = {
        "outcome": result.outcome,
        "escalation_reason": result.escalation_reason,
        "eligible_uins": result.eligible_uins,
    }
    if current.counters.get("express_path"):
        turn.signals["express_path"] = True
    logger.info("eligibility %s: %s, flags %s", result.decision_id, result.outcome, result.flags)
    texts = scripts(turn)
    if result.outcome == "RE_ASK":
        if current.counters.get("occupation_reask", 0) >= 1:
            turn.signals["reask_exhausted"] = True
        elif (attr := screen.attr("occupation_code")) is not None:
            current.counters = {**current.counters, "occupation_reask": 1}
            screen.lead.append(("reask", texts.reask))
            screen.ask(attr)
    elif result.outcome == "NOT_ELIGIBLE":
        reasons = texts.not_eligible_reasons
        line = next((reasons[c] for c in result.reason_codes if c in reasons), reasons["default"])
        turn.parts = [("not_eligible", texts.not_eligible.format(reason=line))]
        turn.quick_replies = []
    elif result.outcome == "ELIGIBLE":
        turn.parts = [("screening_done", texts.screening_done)]
        turn.quick_replies = []


def _payload(request: EligibilityRequest, result: EligibilityResult) -> EligibilityPayload | None:
    """TDD §3.6's EligibilityPayload, held in the session. An age outside its 18-75 band has none
    (the engine escalates it); a declined occupation is the engine's own class, "declined"."""
    try:
        return EligibilityPayload(
            age_years=request.age_years,
            gender=request.gender,
            residency=request.residency,
            pincode=request.pincode,
            tobacco_12m=request.tobacco_12m,
            occupation_class=request.occupation_code or "declined",
            health_flags=request.health_flags,
            confirmed_at=datetime.now(UTC),
            engine=result,
        )
    except ValidationError:
        return None


# --- V4 from S2 and S3 ----------------------------------------------------------------------------
async def correction(turn: Any) -> None:
    """A changed eligibility fact after S1 (TDD §3.1 V4): a corrected row, the engine's result
    dropped, and the `correction` signal, so G2 re-runs S1's rows (s1 enter reads everything back
    and the engine decides again). The S2 and S3 nodes call this first."""
    current = session(turn)
    said = [(ALIASES.get(c.slot, c.slot), c) for c in turn.slots_pending]
    said = [(slot, c) for slot, c in said if slot in ELIGIBILITY]
    if not valid(current) or not said:
        return
    screen = await Screen.load(turn, current, "s1")
    before = dict(screen.known)
    for slot, candidate in said:
        await screen.take(slot, candidate.value, candidate.evidence_span, candidate.confidence)
    if not turn.signals.get("minor") and screen.known != before:
        turn.signals["correction"] = "eligibility"
        logger.info("eligibility fact corrected in %s: S1's rows again", current.fsm_state)
