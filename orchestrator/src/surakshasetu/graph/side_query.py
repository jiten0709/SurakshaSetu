"""The side-query subgraph: TDD §2.6's FAQ Engine, row CC4 (Step 22).

A question outside the pending step never loses the customer's place. The turn router pushes a
frame {state, pending_slot, prompt_id, focus_uins} while the stack is below SS_SIDE_QUERY_MAX_STACK,
and the turn runs in TDD order (graph/nodes.side_query):
1. the origin state's node takes what the turn answered (a compound turn, "I'm 34, and how does 80C
   work here?", fills the age first) and puts its pending prompt again, as after any note;
2. `answer` answers from a frozen view of the session (SessionView: the subgraph reads session
   facts and cannot write them). In S0, before consent, and once the conversation has ended, only
   from the approved privacy FAQ (content/faq), never generated. Elsewhere from the evidence, with
   the side-query L1 on gen-recommend: every claim cited and verified by the output rails, the
   state's caveat after it, and for tax the regime condition and DISC-GLOBAL-TAX-05 verbatim (never
   a personal computation; the regime asked when it is not known);
3. `respond` pops the frame and joins: the answer, the one-line bridge, then the state's prompt.

Insufficient evidence, or retrieval down, abstains ("I can't confirm that") with an advisor offer,
and nothing about the state changes. A full stack answers with the abstain line alone and goes back
to the pending prompt. After SS_SIDE_QUERY_OFFER_AFTER side queries in a row the bridge becomes an
offer to carry on or to talk to an advisor.

A fact revealed in a question ("I'm on the old regime") is never used as said: the node (not the
read-only answer) writes it as a proposed tax_regime slot row with a yes/no quick reply (FACT), and
the confirmation writes the confirmed row. Only a confirmed regime filters the tax evidence.
"""

import dataclasses
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Self, cast
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from surakshasetu.analysis.models import Intent
from surakshasetu.compose.bundle import Approval, Scripts, mentions, phrase
from surakshasetu.compose.citations import TurnHandles, render, source_list
from surakshasetu.compose.composer import Rendered
from surakshasetu.fsm.states import TERMINAL, FsmState
from surakshasetu.graph import handlers
from surakshasetu.graph.handlers import TurnIO, quick_reply, scripts, session
from surakshasetu.graph.state import SessionState, SlotRow
from surakshasetu.graph.states import s3
from surakshasetu.store import conv as store

logger = logging.getLogger(__name__)

FAQ_ROOT = Path(__file__).resolve().parents[4] / "content" / "faq"
IST = ZoneInfo("Asia/Kolkata")
TAX_05 = "DISC-GLOBAL-TAX-05"
TAX = re.compile(r"(?i)\btax(es)?\b|टैक्स")
# "FY 2025-26", "AY 2026-27", "2025-26": the tax year a question is about. An assessment year is
# the tax year before it.
YEAR = re.compile(r"(?i)\b(fy|ay)?\s*(20\d\d)\s*[-–/]\s*(\d\d)\b")
REGIME = "tax_regime"  # the one fact a side query proposes (TDD §2.6's "I'm on the old regime")
COUNTER = "side_queries"  # consecutive side-query turns
Part = tuple[str, str]


# --- the approved privacy FAQ ---------------------------------------------------------------------
class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FaqEntry(_Strict):
    id: str = Field(pattern=r"^PF-[A-Z0-9-]+$")
    topic: str
    terms: frozenset[str]
    answer: str = Field(min_length=1)

    @field_validator("terms", mode="before")
    @classmethod
    def _normalised(cls, entries: list[str]) -> frozenset[str]:
        if not entries or not all(isinstance(e, str) and phrase(e) for e in entries):
            raise ValueError("terms must be non-empty strings")
        return frozenset(phrase(e) for e in entries)


class PrivacyFaq(_Strict):
    version: str = Field(pattern=r"^\d{4}\.\d{2}\.\d+$")
    locale: Literal["en-IN", "hi-IN"]
    is_dummy: bool
    approved_by: list[Approval]
    entries: list[FaqEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def _approved_and_unique(self) -> Self:
        if len({a.by for a in self.approved_by}) < 2:
            raise ValueError("the privacy FAQ needs two distinct approvers")
        if len({e.id for e in self.entries}) != len(self.entries):
            raise ValueError("entry ids must be unique")
        return self

    def match(self, text: str) -> FaqEntry | None:
        """The first entry one of whose terms the question contains, as whole words."""
        return next((e for e in self.entries if mentions(e.terms, text)), None)


class FaqError(Exception):
    """The privacy FAQ cannot be used: NOT_FOUND, INVALID, LOCALE_MISMATCH or DUMMY_REFUSED."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@lru_cache(maxsize=8)
def privacy_faq(locale: str, env: str, root: Path = FAQ_ROOT) -> PrivacyFaq:
    """content/faq/privacy-<locale>.yaml, validated once per process. Not a session pin (decided
    2026-10-05): each answer's part id and RESPONSE_RELEASED header name the version and entry."""
    path = root / f"privacy-{locale}.yaml"
    if not path.is_file():
        raise FaqError("NOT_FOUND")
    try:
        faq = PrivacyFaq.model_validate(yaml.safe_load(path.read_text("utf-8")))
    except (ValidationError, yaml.YAMLError) as exc:
        raise FaqError("INVALID") from exc
    if faq.locale != locale:
        raise FaqError("LOCALE_MISMATCH")
    if faq.is_dummy and env in ("pilot", "prod"):
        raise FaqError("DUMMY_REFUSED")
    logger.info("privacy FAQ %s %s loaded: %d entries", locale, faq.version, len(faq.entries))
    return faq


# --- the frozen view ------------------------------------------------------------------------------
class SessionView(SessionState):
    """The session as the subgraph may see it: a copy, frozen. It can read every fact and write
    none; a nested write lands on the copy, never on the turn's working session."""

    model_config = ConfigDict(frozen=True)


def view_of(current: SessionState) -> SessionView:
    return SessionView.model_validate(current.model_dump())


# --- the answer -----------------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class SideAnswer:
    """What the subgraph answers; the node puts it on the turn. Template answers have no handles;
    a cited answer has the draft, the handles its envelope carried and the regeneration."""

    outcome: str  # answered, abstained, unavailable, stack_full, faq:<id>, none (header)
    lead: tuple[Part, ...] = ()  # before the answer's body (template answers: the body)
    after: tuple[Part, ...] = ()  # the caveat, the tax parts, the exclusion note
    draft: str | None = None
    handles: TurnHandles | None = None
    regenerate: Callable[[list[str]], Awaitable[str | None]] | None = None
    shown: tuple[str, ...] = ()  # approved text released verbatim (RC-LEAK accepts it)
    quick: tuple[dict[str, Any], ...] = ()


def tax_year(question: str, now: datetime) -> str:
    """The tax year the question names, else the current one on the IST calendar (from 1 April),
    so a question about this year cites the Income-tax Act 2025 (in force from 1 April 2026)."""
    if found := YEAR.search(question):
        prefix, start, end = found.group(1), int(found.group(2)), int(found.group(3))
        if end == (start + 1) % 100:
            if (prefix or "").casefold() == "ay":
                start -= 1
            return f"{start}-{(start + 1) % 100:02d}"
    today = now.astimezone(IST).date()
    start = today.year if today.month >= 4 else today.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


def _caveat(view: SessionView, texts: Scripts) -> Part:
    state = view.fsm_state
    if state is FsmState.PAUSE and view.stack:
        state = view.stack[0].state  # the paused-from state's caveat
    return (
        "side_query_caveat",
        texts.side_query_caveat.get(state.value, texts.side_query_caveat["S1"]),
    )


def faq_only(view: SessionView) -> bool:
    """S0, and a conversation that has ended (TDD §3.9: "public FAQ still answered"): the privacy
    FAQ alone, never retrieval or generation."""
    return view.fsm_state is FsmState.S0 or view.fsm_state in TERMINAL


def faq(view: SessionView, io: TurnIO, question: str) -> SideAnswer:
    texts = io.bundle.templates[view.locale].scripts
    found = privacy_faq(view.locale, io.settings.env).match(question)
    if found is None:
        return SideAnswer("faq:none", lead=(("side_query_caveat", texts.side_query_caveat["S0"]),))
    version = privacy_faq(view.locale, io.settings.env).version
    logger.info("side query answered from the privacy FAQ: %s", found.id)
    return SideAnswer(
        f"faq:{found.id}",
        lead=((f"faq:{version}:{found.id}", found.answer),),
        shown=(found.answer,),
    )


async def answer(
    view: SessionView,
    io: TurnIO,
    question: str,
    *,
    intents: list[Intent],
    regime: Literal["old", "new"] | None,
    lead: tuple[Part, ...] = (),
    entities: tuple[str, ...] = (),
    dispute: bool = False,
) -> SideAnswer:
    """A cited answer from the evidence: retrieval for the question (the view's state, the products
    in focus or shown, the regime and the tax year), the side-query L1 on gen-recommend, engine
    facts in S3 only. Tax adds the regime condition (and the regime question when it is not known)
    and DISC-GLOBAL-TAX-05 verbatim; a dispute adds the exclusion note. Insufficient evidence or
    retrieval down: the abstain line and an advisor."""
    texts = io.bundle.templates[view.locale].scripts
    rec = view.recommendation if view.fsm_state is FsmState.S3 else None
    focus = [o.uin for o in rec.options] if rec else list(view.focus_uins)
    retrieval = await s3.retrieve(
        io,
        view,
        question,
        focus_uins=focus,
        entities=list(entities),
        intents=intents,
        regime=regime,
        tax_year=tax_year(question, handlers.now()),
    )
    after: list[Part] = [_caveat(view, texts)]
    shown: list[str] = []
    quick: list[dict[str, Any]] = []
    if (retrieval is not None and retrieval.audit.route_rule == "RT-TAX") or TAX.search(question):
        disclosure = await io.domain.get_disclosure(TAX_05, view.locale)
        shown.append(disclosure.body)
        after.append(("tax_condition", texts.tax_condition))
        if regime is None:
            after.append(("regime_ask", texts.regime_ask))
            quick = [
                quick_reply(label, "FACT", {"slot": REGIME, "value": value, "confirmed": True})
                for value, label in texts.side.regimes.items()
            ]
        after.append((f"registry:{TAX_05}", disclosure.body))
    if dispute:
        after.append(("exclusion_note", texts.exclusion_note))
    advisor = quick_reply(texts.screening.advisor, "HUMAN_REQUEST", {})
    if retrieval is None or retrieval.abstained:
        outcome = "unavailable" if retrieval is None else "abstained"
        logger.info("side query: %s, no answer from the evidence", outcome)
        return SideAnswer(
            outcome,
            lead=(*lead, ("abstain", texts.abstain)),
            after=tuple(after),
            shown=tuple(shown),
            quick=(*quick, advisor),
        )
    facts = []
    if rec is not None and view.suitability is not None:
        facts = s3.engine_facts(texts.s3, view.suitability, rec.options, io.products)
    draft, handles = await s3.generate(io, view, "side-query", retrieval, facts)

    async def regenerate(errors: list[str]) -> str | None:
        return (await s3.generate(io, view, "side-query", retrieval, facts, errors))[0]

    logger.info("side query answered from %d evidence chunks", len(retrieval.evidence))
    return SideAnswer(
        "answered",
        lead=lead,
        after=tuple(after),
        draft=draft,
        handles=handles,
        regenerate=regenerate,
        shown=tuple(shown),
        quick=tuple(quick),
    )


def compose(
    answered: SideAnswer, texts: Scripts, sources_template: Any, resume: Callable[[], list[Part]]
) -> Callable[[str | None], Rendered]:
    """The reply: the answer (cited, or the abstain line when no draft passes the rails), its
    after-parts, the sources, then `resume()`: the bridge and the state's prompt, read when the
    reply is rendered, after an enter hook has put the prompt of a state the turn moved to."""

    def render_(narrative: str | None) -> Rendered:
        body: list[Part] = []
        citations: dict[str, str] = {}
        sources: list[Any] = []
        cited_parts: list[Part] = []
        if answered.handles is not None:
            if narrative is None:
                body = [("abstain", texts.abstain)]
            else:
                cited = render(narrative, answered.handles)
                body = [("generated:answer", cited.text)]
                citations = answered.handles.evidence_map(cited.handles)
                sources = cited.sources
                if cited.sources:
                    cited_parts = [("sources", source_list(cited.sources, sources_template))]
        parts = [
            (i if ":" in i else f"template:{i}", t)
            for i, t in [*answered.lead, *body, *answered.after, *cited_parts, *resume()]
        ]
        text = "\n\n".join(t for _, t in parts)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return Rendered(text, digest, parts, citations, sources, {}, {})

    return render_


# --- the node's side: frames, counters, the revealed fact, the reply ------------------------------
def regime_said(io: TurnIO, text: str) -> Literal["old", "new"] | None:
    """A regime the customer stated: an nlu-extract candidate for tax_regime with its evidence, or
    the bundle's phrases."""
    pipeline = io.pipeline
    if pipeline is not None and pipeline.analysis is not None:
        for c in pipeline.analysis.slots:
            if c.slot == REGIME and c.value in ("old", "new") and c.evidence_span in text:
                return cast(Literal["old", "new"], c.value)
    lexicon = io.bundle.side_query_lexicon
    old, new = mentions(lexicon.regime_old, text), mentions(lexicon.regime_new, text)
    return "old" if old and not new else "new" if new and not old else None


def confirmed_regime(turn: Any, current: SessionState) -> Literal["old", "new"] | None:
    if current.consent is None or not current.consent.valid_p1:
        return None  # no slot is read or written before a valid P1 (I1)
    row = cast(Any, turn.row)
    held = store.latest_slots(turn.conn, turn.keys, row.key_ref, turn.session_id).get(REGIME)
    if held is None or held[0] != "confirmed" or held[1] not in ("old", "new"):
        return None
    return cast(Literal["old", "new"], held[1])


def fact_action(turn: Any) -> list[Part]:
    """FACT {slot, value, confirmed}: the customer's answer to a proposed fact or to the regime
    question. Confirmed -> a confirmed row; declined -> a declined row. Only tax_regime, only with
    a valid P1 (I1). Returns the acknowledgement part."""
    current = session(turn)
    payload = (turn.action or {}).get("payload") or {}
    slot, value, confirmed = payload.get("slot"), payload.get("value"), payload.get("confirmed")
    if (
        slot != REGIME
        or value not in ("old", "new")
        or not isinstance(confirmed, bool)
        or current.consent is None
        or not current.consent.valid_p1
    ):
        logger.warning("a FACT action that proposes nothing: ignored")
        return []
    texts = scripts(turn)
    if not confirmed:
        turn.slot_rows.append(SlotRow(REGIME, None, 1.0, "declined"))
        logger.info("proposed tax regime declined")
        return []
    turn.slot_rows.append(SlotRow(REGIME, value, 1.0, "confirmed"))
    logger.info("tax regime confirmed")
    return [("regime_noted", texts.regime_noted.format(regime=texts.side.regimes[value]))]


async def respond(
    turn: Any,
    question: str,
    *,
    lead: tuple[Part, ...] = (),
    entities: tuple[str, ...] = (),
    counted: bool = True,
) -> None:
    """After the origin state's node (graph/nodes.side_query): the answer from the frozen view,
    the revealed fact proposed, the reply joined, and the frame popped. The state's own reply is
    kept as the resume (its parts and quick replies). The objection handler answers through it too
    (a trust or guarantee objection, cited), with its own lead, not counted as a side query."""
    current = session(turn)
    texts = scripts(turn)
    io = handlers.io(turn)
    frame = turn.frame
    if frame is not None and current.stack and current.stack[-1] == frame:
        current.stack = current.stack[:-1]  # back to exactly where the customer was
    if turn.answered or turn.render is not None or turn.degraded or _speaks_elsewhere(turn):
        # The state answered it itself (S3's "which one"), composed its own reply (the options
        # presented again), or compose speaks (degraded, overlong, identity, safety).
        turn.side_outcome = "state"
        logger.info("side query left to the state's own reply")
        return
    view = view_of(current)
    count = current.counters.get(COUNTER, 0) + (1 if counted else 0)
    regime = confirmed_regime(turn, current)
    if turn.side_full:
        answered = SideAnswer("stack_full", lead=(("abstain", texts.abstain),))
    elif faq_only(view):
        answered = faq(view, io, question)
    else:
        text = io.pipeline.stored_raw if io.pipeline is not None else question
        dispute = view.fsm_state is FsmState.S3 and (
            mentions(io.bundle.s3_lexicon.exclusion_dispute, text)
            or mentions(io.bundle.side_query_lexicon.pushback, text)
        )
        answered = await answer(
            view,
            io,
            question,
            intents=list(io.pipeline.analysis.intents)
            if io.pipeline and io.pipeline.analysis
            else [],
            regime=regime,
            lead=lead,
            entities=entities,
            dispute=dispute,
        )
        said = regime_said(io, text)
        proposed = said if said != regime else None
        if proposed is not None and current.consent is not None and current.consent.valid_p1:
            # Revealed in the question: proposed, never used as said (TDD §2.6).
            turn.slot_rows.append(SlotRow(REGIME, proposed, 1.0, "proposed"))
            label = texts.side.regimes[proposed]
            answered = dataclasses.replace(
                answered,
                after=tuple(p for p in answered.after if p[0] != "regime_ask")
                + (("regime_confirm", texts.regime_confirm.format(regime=label)),),
                quick=(
                    quick_reply(texts.side.confirm, "FACT", _fact(proposed, True)),
                    quick_reply(texts.side.decline, "FACT", _fact(proposed, False)),
                    *(q for q in answered.quick if q["action"]["type"] != "FACT"),
                ),
            )
            logger.info("a tax regime revealed in a question: proposed")
    resume: Callable[[], list[Part]]
    if counted and count >= turn.settings.side_query_offer_after:
        offer: list[Part] = [("side_query_offer", texts.side_query_offer)]
        resume = lambda: offer  # noqa: E731
        quick = [
            quick_reply(texts.side.keep_going, "CONTINUE", {}),
            quick_reply(texts.screening.advisor, "HUMAN_REQUEST", {}),
        ]
        count = 0
        logger.info("side queries in a row: carry on or an advisor offered")
    else:
        bridge: Part = ("side_query_bridge", texts.side_query_bridge)
        resume = lambda: [bridge, *turn.parts] if turn.parts else []  # noqa: E731
        held = {q["action"]["type"] for q in turn.quick_replies}
        quick = [q for q in answered.quick if q["action"]["type"] not in held]
        quick += turn.quick_replies
    if counted:
        current.counters = {**current.counters, COUNTER: count}
    sources_template = io.bundle.templates[current.locale].recommendation
    turn.render = compose(answered, texts, sources_template, resume)
    turn.draft, turn.regenerate, turn.handles = (
        answered.draft,
        answered.regenerate,
        answered.handles,
    )
    turn.verify_all = answered.handles is not None
    turn.shown.extend(answered.shown)
    turn.quick_replies, turn.phrase = quick, None
    turn.side_outcome = answered.outcome


def _fact(value: str, confirmed: bool) -> dict[str, Any]:
    return {"slot": REGIME, "value": value, "confirmed": confirmed}


def _speaks_elsewhere(turn: Any) -> bool:
    pipeline = turn.pipeline
    return bool(turn.identity or turn.safety or (pipeline is not None and pipeline.overlong))
