"""The S0 node: State-0, greeting, AI disclosure and consent (TDD §3.5; Step 18).

S0 generates nothing. It releases bundle templates, the registry's DISC-GLOBAL-AI-06 and the
Consent Service's notice body, verbatim, and calls no gen-* route. Consent is recorded only by
the Consent Service, from the structured CONSENT_SUBMIT action or, while the consent prompt is
open, from a whole-message match in the bundle's closed lexicon
(lexicons/consent_affirmation.yaml): an affirmation grants P1 only, after a separate typed 18+
question. Anything else is not consent.

The node never picks the next state. It records what the customer did, as the session's consent
record or as a Facts signal in turn.signals (a refusal, an under-18 declaration, a quick-reply
intent, the default intent), and decide applies S0's rows: CC FAQ, 1 existing_policy -> Human
Escalation, 2 refused -> Exit, 3 specific_plan -> Quote-Only, 4 new_purchase -> S1.

The open prompt is session.last_prompt_id: None (the greeting not shown yet), s0.consent, s0.age
(the typed path's 18+ question) or s0.intent. It lives in the checkpoint; a hydrated session loses
it and gets the greeting again, which is safe. Clarifications of the intent are counted in
counters["intent_clarify"].

Nothing the customer says in S0 is slot-filled (I1). Slot candidates are dropped from the turn, so
conv.turn.analysis keeps none of them; their names stay on the turn (memory only), and S1 asks every
attribute again after consent. A stated age under 18 is used once as the minor signal (V2), then
dropped with the rest.

`enter` renders the open prompt when another state's turn re-enters S0 (G1: the consent lapsed or
the notice was superseded, often on resume).
"""

import dataclasses
import logging
from datetime import datetime
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from surakshasetu.analysis.models import Intent, SlotCandidate
from surakshasetu.analysis.normalisers import parse_age
from surakshasetu.audit.events import ConsentCapturedHeader, EventType, Sha256Hex
from surakshasetu.compose.bundle import phrase
from surakshasetu.domain.client import DomainError
from surakshasetu.domain.models import ConsentMethod, ConsentRecordCreate, PurposeGrant, PurposeId
from surakshasetu.fsm.facts import S0Intent
from surakshasetu.graph.handlers import (
    append,
    bundle,
    granted,
    identity,
    quick_reply,
    scripts,
    session,
)
from surakshasetu.graph.state import GraphState, SessionState
from surakshasetu.store.conv import SessionRow

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
Purpose = Literal["P1", "P2", "P3"]
PURPOSES: dict[Purpose, PurposeId] = {
    "P1": "P1_NEEDS_RECO",
    "P2": "P2_ADVISOR_CONTACT",
    "P3": "P3_MARKETING",
}
Locale = Literal["en-IN", "hi-IN"]
LOCALES: tuple[Locale, ...] = ("en-IN", "hi-IN")
PROMPT_CONSENT, PROMPT_AGE, PROMPT_INTENT = "s0.consent", "s0.age", "s0.intent"
CLARIFY = "intent_clarify"
CLARIFY_LIMIT = 2  # TDD §3.5: clarify the intent at most twice, then default to new_purchase
S0_INTENTS = {Intent.NEW_PURCHASE, Intent.SPECIFIC_PLAN, Intent.EXISTING_POLICY}
Lead = Literal["greeting", "consent_renew", "notice_updated", "consent_reprompt"]


class ConsentSubmit(BaseModel):
    """CONSENT_SUBMIT's payload: every purpose ticked or not, the 18+ box, and the notice shown."""

    model_config = ConfigDict(extra="forbid")

    purposes: dict[Purpose, bool]
    age_18_plus: bool
    notice_version: str = Field(min_length=1, max_length=64)
    notice_sha256: Sha256Hex

    @model_validator(mode="after")
    def _every_purpose(self) -> "ConsentSubmit":
        if set(self.purposes) != set(PURPOSES):
            raise ValueError("P1, P2 and P3 must each be answered")
        return self


class IntentChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: S0Intent


def valid(current: SessionState) -> bool:
    return current.consent is not None and current.consent.valid_p1


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    current = session(turn)
    action = turn.action or {}
    _hold_back(turn)
    if action:
        await _action(turn, current, str(action.get("type")), action.get("payload") or {})
    elif valid(current):
        _intent_text(turn, current)
    else:
        await _consent_text(turn, current)


async def enter(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """Another state's turn re-entered S0: show the prompt that is open for the consent held."""
    turn = runtime.context
    current = session(turn)
    if valid(current):
        intent_prompt(turn, current, lead=True)
    else:
        await consent_prompt(turn, "consent_renew" if current.consent else "greeting")
    logger.info("S0 re-entered: %s", current.last_prompt_id)


# --- what the customer did ------------------------------------------------------------------------
async def _action(turn: Any, current: SessionState, kind: str, payload: dict[str, Any]) -> None:
    if kind == "INTENT":
        try:
            choice = IntentChoice.model_validate(payload).intent
        except ValidationError:
            logger.warning("malformed INTENT action: prompt re-presented")
            await _reprompt(turn, current)
            return
        turn.signals["intent"] = choice
        if choice == "general_faq":
            turn.signals["faq"] = True
            _faq(turn, current)
            if valid(current):
                intent_prompt(turn, current, lead=False)
            else:
                await consent_prompt(turn, None)
        elif choice != "existing_policy" and not valid(current):
            await consent_prompt(turn, "consent_reprompt")  # S0.3/S0.4 need a valid P1
        return
    if valid(current):  # START, a repeated submit, a language switch: the intent question stands
        intent_prompt(turn, current, lead=True)
        return
    if kind == "START":
        await consent_prompt(turn, "greeting")
    elif kind == "NOTICE_LANGUAGE":
        language = payload.get("language")
        if language in LOCALES:
            current.locale = str(language)
            turn.ai_disclosure = None  # the AI disclosure in the new language
            logger.info("notice language switched to %s", language)
        await consent_prompt(turn, "greeting")
    elif kind == "CONSENT_SUBMIT":
        await _submit(turn, current, payload)
    else:
        logger.debug("action %s ignored in S0", kind)
        await _reprompt(turn, current)


async def _submit(turn: Any, current: SessionState, payload: dict[str, Any]) -> None:
    try:
        choice = ConsentSubmit.model_validate(payload)
    except ValidationError:
        logger.warning("malformed CONSENT_SUBMIT: options re-presented")
        await consent_prompt(turn, "consent_reprompt")
        return
    if not choice.purposes["P1"]:
        refuse(turn)
    elif not choice.age_18_plus:  # P1 granted, the 18+ box unticked: under 18 (V2)
        turn.signals["minor"] = True
        logger.info("under 18 declared on the consent form: nothing recorded")
    else:
        await _record(
            turn,
            current,
            purposes=choice.purposes,
            notice=(choice.notice_version, choice.notice_sha256),
            method="structured_action",
        )


async def _consent_text(turn: Any, current: SessionState) -> None:
    """Free text with no valid P1. A minor signal leaves the reply to the erasure handler."""
    pipeline = turn.pipeline
    if turn.signals.get("minor"):
        return
    if current.last_prompt_id is None:  # the first turn, whatever it says: the greeting
        await consent_prompt(turn, "greeting")
    elif pipeline is None or pipeline.blocked or pipeline.overlong or turn.identity:
        # Injection ignored, options re-presented; or compose speaks (shorten, AI re-disclosure).
        lead: Lead | None = "consent_reprompt" if pipeline and pipeline.blocked else None
        await consent_prompt(turn, lead)
    elif _asked_faq(turn):
        _faq(turn, current)
        await consent_prompt(turn, None)
    elif current.last_prompt_id == PROMPT_AGE:
        await _age_answer(turn, current, phrase(pipeline.stored_raw))
    elif current.last_prompt_id == PROMPT_CONSENT:
        said = phrase(pipeline.stored_raw)
        lexicon = bundle(turn).consent_lexicon
        if said in lexicon.affirm:
            turn.parts = [("age_confirm_ask", scripts(turn).age_confirm_ask)]
            current.last_prompt_id = PROMPT_AGE
        elif said in lexicon.decline:
            refuse(turn)
        else:
            await consent_prompt(turn, "consent_reprompt")
    else:  # the intent was asked, but the consent has lapsed since
        await consent_prompt(turn, "consent_renew")


async def _age_answer(turn: Any, current: SessionState, said: str) -> None:
    lexicon = bundle(turn).consent_lexicon
    if said in lexicon.adult:
        try:
            notice = await turn.domain.get_current_consent_notice(current.locale)
        except DomainError as exc:
            _outage(turn, exc)
            return
        await _record(
            turn,
            current,
            purposes={"P1": True, "P2": False, "P3": False},
            notice=(notice.notice_version, notice.body_sha256),
            method="parsed_affirmation",
        )
    elif said in lexicon.minor:
        turn.signals["minor"] = True
        logger.info("under 18 declared to the typed 18+ question: nothing recorded")
    else:  # not a confirmation: back to the options
        await consent_prompt(turn, "consent_reprompt")


def _intent_text(turn: Any, current: SessionState) -> None:
    """Free text with a valid P1: the analysis's intent moves the session on; otherwise clarify,
    twice at most, then default to new_purchase (TDD §3.5)."""
    pipeline = turn.pipeline
    analysis = pipeline.analysis if pipeline else None
    intents = set(analysis.intents) if analysis else set()
    if turn.signals.get("minor") or intents & S0_INTENTS:
        return
    if _asked_faq(turn):
        _faq(turn, current)
        intent_prompt(turn, current, lead=False)
    elif pipeline is not None and (pipeline.blocked or pipeline.overlong or turn.identity):
        intent_prompt(turn, current, lead=pipeline.blocked)
    elif current.counters.get(CLARIFY, 0) >= CLARIFY_LIMIT:
        turn.signals["intent"] = "new_purchase"
        logger.info(
            "CONFIG_NOTE: intent defaulted to new_purchase after %d clarifications; the default"
            " can route a servicing customer into sales, an open point for the specification"
            " owner (TDD §3.5)",
            CLARIFY_LIMIT,
        )
    else:
        current.counters = {**current.counters, CLARIFY: current.counters.get(CLARIFY, 0) + 1}
        texts = scripts(turn)
        turn.parts = [("clarify", texts.clarify.format(question=texts.intent_ask))]
        intent_prompt(turn, current, lead=False)


def _asked_faq(turn: Any) -> bool:
    analysis = turn.pipeline.analysis if turn.pipeline else None
    return analysis is not None and Intent.GENERAL_FAQ in analysis.intents


def _faq(turn: Any, current: SessionState) -> None:
    """CC4 in S0: privacy questions only, and until Step 22's privacy FAQ, the caveat alone."""
    turn.parts = [("side_query_caveat", scripts(turn).side_query_caveat["S0"])]


def refuse(turn: Any) -> None:
    turn.signals["consent"] = "refused"
    turn.parts = [("consent_declined", scripts(turn).consent_declined)]
    logger.info("P1 refused in S0: nothing collected")


# --- recording consent ----------------------------------------------------------------------------
async def _record(
    turn: Any,
    current: SessionState,
    *,
    purposes: dict[Purpose, bool],
    notice: tuple[str, str],
    method: ConsentMethod,
) -> None:
    """The Consent Service records it (the orchestrator never writes consent). Idempotent on the
    turn's key, so a retried turn cannot record twice."""
    row = cast(SessionRow, turn.row)
    request = ConsentRecordCreate(
        session_id=turn.session_id,
        subject_ref=row.subject_ref,
        notice_version=notice[0],
        notice_sha256=notice[1],
        language=current.locale,
        ai_disclosure_version=current.pins.registry,
        purposes=[PurposeGrant(purpose_id=pid, granted=purposes[p]) for p, pid in PURPOSES.items()],
        age_18_plus_declared=True,
        method=method,
    )
    try:
        record = await turn.domain.create_consent_record(request, str(turn.turn_key))
    except DomainError as exc:
        if exc.code == "NOTICE_MISMATCH":
            logger.info("consent submitted on a notice not in force: the current one re-rendered")
            await consent_prompt(turn, "notice_updated")
            return
        _outage(turn, exc)
        return
    if not record.valid_p1:
        logger.warning("consent recorded but not valid: %s", ",".join(record.valid_reasons))
        await consent_prompt(turn, "consent_reprompt")
        return
    current.consent = record
    if current.pins.consent_notice != record.notice_version:
        # I7's exception (decided 2026-10-04): the pin follows the notice consented to.
        current.pins = current.pins.model_copy(update={"consent_notice": record.notice_version})
        logger.info("consent notice re-pinned at capture")
    append(
        turn,
        EventType.CONSENT_CAPTURED,
        ConsentCapturedHeader(
            consent_id=record.consent_id,
            notice_version=record.notice_version,
            notice_sha256=record.notice_sha256,
            purposes=cast(Any, granted(record.purposes)),
            method=method,
            language=record.notice_language,
            adult_declared=record.age_18_plus_declared,
            captured_at=record.captured_at,
        ),
        {"record": record.model_dump(mode="json")},
    )
    logger.info("consent captured: %s, purposes %s", method, ",".join(granted(record.purposes)))
    intent_prompt(turn, current, lead=True)


def _outage(turn: Any, exc: DomainError) -> None:
    """The Consent Service or the registry is down: never proceed without a record."""
    if exc.status is not None and exc.status < 500:
        raise exc  # a contract problem, not an outage
    logger.warning("consent dependency down in S0: %s; retry offered", exc.code)
    texts = scripts(turn)
    turn.parts = [("consent_retry", texts.consent_retry)]
    turn.form = None
    turn.quick_replies = [quick_reply(texts.consent_form.retry, "START", {})]


# --- prompts --------------------------------------------------------------------------------------
async def consent_prompt(turn: Any, lead: Lead | None) -> None:
    """The consent prompt: the lead template; with a greeting, renewal or updated notice also the
    AI disclosure and the notice in force for the session's language, verbatim; and the form for
    that notice. lead None keeps the turn's own parts and re-attaches the form only."""
    current = session(turn)
    try:
        notice = await turn.domain.get_current_consent_notice(current.locale)
    except DomainError as exc:
        _outage(turn, exc)
        return
    texts = scripts(turn)
    if lead == "consent_reprompt":
        turn.parts = [(lead, texts.consent_reprompt)]
    elif lead is not None:
        ai = await identity.disclosure(turn)
        if ai is None:
            _outage(turn, DomainError("UNAVAILABLE", None))
            return
        opening = {
            "greeting": texts.greeting.format(insurer=bundle(turn).manifest.insurer),
            "consent_renew": texts.consent_renew,
            "notice_updated": texts.notice_updated,
        }[lead]
        turn.parts = [
            (lead, opening),
            (f"registry:{identity.AI_DISCLOSURE}", ai),
            (f"notice:{notice.notice_version}", notice.body),
        ]
        turn.shown.append(notice.body)
    labels = texts.consent_form
    turn.form = {
        "type": "CONSENT_SUBMIT",
        "notice_version": notice.notice_version,
        "notice_sha256": notice.body_sha256,
        "language": current.locale,
        "purposes": [
            {"id": p, "label": labels.purposes[p], "required": p == "P1"} for p in PURPOSES
        ],
        "adult": {"label": labels.adult},
        "submit": labels.submit,
    }
    turn.quick_replies = [
        quick_reply(labels.languages[loc], "NOTICE_LANGUAGE", {"language": loc})
        for loc in LOCALES
        if loc != current.locale
    ]
    current.last_prompt_id = PROMPT_CONSENT


async def _reprompt(turn: Any, current: SessionState) -> None:
    if valid(current):
        intent_prompt(turn, current, lead=True)
    else:
        await consent_prompt(turn, "consent_reprompt")


def intent_prompt(turn: Any, current: SessionState, *, lead: bool) -> None:
    """The four quick replies (TDD §3.5), with the intent question when lead is set."""
    texts = scripts(turn)
    if lead:
        turn.parts = [("intent_ask", texts.intent_ask)]
    turn.form = None
    turn.quick_replies = [
        quick_reply(label, "INTENT", {"intent": intent}) for intent, label in texts.intents.items()
    ]
    current.last_prompt_id = PROMPT_INTENT


# --- before consent: nothing is slot-filled -------------------------------------------------------
def _hold_back(turn: Any) -> None:
    """S0 fills no slot (I1). A stated age under 18 is the minor signal (V2); every candidate is
    then dropped from the turn, its slot name kept for this turn only."""
    pipeline = turn.pipeline
    if pipeline is None or pipeline.analysis is None or not pipeline.analysis.slots:
        return
    slots = pipeline.analysis.slots
    if any(_minor_age(s) for s in slots):
        turn.signals["minor"] = True
        logger.info("under 18 stated in S0: nothing recorded")
    turn.volunteered = [s.slot for s in slots]
    turn.slots_pending = []
    analysis = pipeline.analysis.model_copy(update={"slots": []})
    turn.pipeline = dataclasses.replace(pipeline, analysis=analysis)
    logger.debug("%d details volunteered in S0: not filled", len(slots))


def _minor_age(slot: SlotCandidate) -> bool:
    if not slot.slot.startswith("age"):
        return False
    stated = parse_age(slot.evidence_span, current_year=datetime.now(IST).year)
    return stated is not None and stated.value is not None and stated.value < 18
