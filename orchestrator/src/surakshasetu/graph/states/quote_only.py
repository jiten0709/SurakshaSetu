"""The QUOTE_ONLY node: the Quote-Only express path (TDD §3.6, after the S1 tables; Step 19).

An indicative premium for a plan the customer names, from age, tobacco (and gender, only where the
rules' rating basis uses it). Entered from S0 (specific_plan) or S1 (eligible + express path). The
plan must be named by the customer: by its catalog name or its UIN, never proposed by the assistant
(I3). The values come through S1's Screen (one question per turn, evidence-spanned, read back
before the quote when collected here). The quote is the quote adapter's: POST /v1/quotes at the
product's quote_defaults (GET /v1/catalog/products/{uin}), or the cover and term the customer
stated. The card is a bundle template filled from the quote's own response, with
DISC-GLOBAL-QUOTE-02 verbatim from the registry and the standing Quote-Only caveat.

Rows (decide): QO.1 any request to apply -> the hard block, state unchanged (C13); QO.2 satisfied ->
Exit with a summary (and the re-engagement line with P3 only); QO.3 an opt-in -> S2 when eligibility
is established, else QO.3b -> S1 to finish it. There is no path to an application.

Problems: 422 QUOTE_OUT_OF_BOUNDS re-asks the cover or term with the problem's own bounds (an entry
age outside the plan's band asks for another plan); 409 PRODUCT_WITHDRAWN and 404 say the plan is
not available and ask for another; 503 or the tier down gives the "rating unavailable" template and
a retry. A declined tobacco answer gets no premium and no quote call.
"""

import logging
from datetime import date
from typing import Any

from langgraph.runtime import Runtime

from surakshasetu.analysis.models import Intent
from surakshasetu.audit.events import EngineDecisionHeader, EventType
from surakshasetu.compose.bundle import mentions
from surakshasetu.compose.placeholders import format_inr
from surakshasetu.domain.client import DomainError
from surakshasetu.domain.models import Pins, Problem, Product, QuoteRequest
from surakshasetu.graph.handlers import (
    append,
    bundle,
    granted,
    product_names,
    quick_reply,
    scripts,
    session,
)
from surakshasetu.graph.state import GraphState, SessionState
from surakshasetu.graph.states.s1 import Screen, outage, said, valid
from surakshasetu.rails.normalise import normalise
from surakshasetu.rails.output import products_named

logger = logging.getLogger(__name__)

QUOTE_SLOTS = ("age_years", "gender", "tobacco_12m")  # of the rules' attributes, what a quote needs
STATED = ("sum_assured_inr", "term_years")  # what a customer may state for the quote
PLAN, QUOTED, BLOCKED = "qo.plan", "qo.quoted", "qo.blocked"
QUOTE_02 = "DISC-GLOBAL-QUOTE-02"


async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    current = session(turn)
    kind = (turn.action or {}).get("type")
    if not valid(current) or kind == "HUMAN_REQUEST":
        return  # G1 re-enters S0 (I1); CC2 hands over
    if turn.pipeline is not None and turn.pipeline.overlong:
        return
    if kind == "OPT_IN":
        _opt_in(turn, current)
        return
    if kind == "SATISFIED":
        await _satisfied(turn, current)
        return
    text = turn.pipeline.stored_raw if turn.pipeline is not None else None
    if text is not None and mentions(bundle(turn).screening_lexicon.apply, text):
        _hard_block(turn, current)  # QO.1 (C13)
        return
    if current.last_prompt_id in (QUOTED, BLOCKED) and text is not None:
        answer = said(turn)
        analysis = turn.pipeline.analysis if turn.pipeline is not None else None
        declined = analysis is not None and Intent.DECLINE in analysis.intents
        if answer is True:
            _opt_in(turn, current)
            return
        if (answer is False or declined) and current.last_prompt_id == QUOTED:
            await _satisfied(turn, current)
            return
    try:
        screen = await _screen(turn, current)
        await _plan(screen)
        ready = await screen.respond()
        if screen.filled:
            current.quote = None  # a changed age, tobacco answer, cover or term: quote again
        if not current.focus_uins:
            _ask_plan(screen)
        elif ready:
            await _quote(screen)
    except DomainError as exc:
        outage(turn, exc, ("rating_unavailable", scripts(turn).rating_unavailable))


async def enter(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """Entered from S0 (specific_plan) or S1 (eligible + express path): the plan if this turn
    named one, then the next question, the read-back, or the quote."""
    turn = runtime.context
    current = session(turn)
    current.counters = {k: v for k, v in current.counters.items() if k != "express_path"}
    try:
        screen = await _screen(turn, current)
        await _plan(screen)
        ready = await screen.next()
        if not current.focus_uins:
            _ask_plan(screen)
        elif ready:
            await _quote(screen)
    except DomainError as exc:
        outage(turn, exc, ("rating_unavailable", scripts(turn).rating_unavailable))
    logger.info("Quote-Only entered: %s", current.last_prompt_id)


async def _screen(turn: Any, current: SessionState) -> Screen:
    return await Screen.load(turn, current, "qo", only=QUOTE_SLOTS, extra=STATED)


async def _plan(screen: Screen) -> None:
    """The plan the customer named, by catalog name or UIN (rails.output.products_named, as I3's
    output rail reads names). A new plan drops the last quote."""
    turn, current = screen.turn, screen.session
    if turn.pipeline is None:
        return
    turn.products = await product_names(turn)
    named = products_named(normalise(turn.pipeline.stored_raw).text, turn.products)
    if len(named) == 1 and (uin := next(iter(named))) not in current.focus_uins:
        current.focus_uins = [uin]
        current.quote = None
        screen.noted = True  # a plan, not an answer to the open question
        logger.info("Quote-Only plan named")


def _ask_plan(screen: Screen) -> None:
    """I3: the assistant never proposes a plan; the customer names one."""
    screen.session.pending_slot = None
    screen.session.last_prompt_id = PLAN
    screen.turn.phrase = None
    screen.reply([("plan_ask", scripts(screen.turn).plan_ask)], [])


async def _quote(screen: Screen) -> None:
    turn, current = screen.turn, screen.session
    texts = scripts(turn)
    values = screen.values()
    uin = current.focus_uins[0]
    if values.get("tobacco_12m") is None:  # declined: no premium and no quote call
        _next_step(screen, ("quote_withheld", texts.quote_withheld))
        return
    if current.quote is not None and current.quote.uin == uin:  # nothing changed since
        _next_step(screen, ("quote_next", texts.quote_next))
        return
    try:
        product = await turn.domain.get_product(uin)
    except DomainError as exc:
        if exc.code != "NOT_FOUND":
            raise
        _unavailable(screen, ("plan_unknown", texts.plan_unknown))
        return
    defaults = product.quote_defaults
    request = QuoteRequest(
        pins=Pins(rules=current.pins.rules),
        uin=uin,
        sum_assured_inr=str(values.get("sum_assured_inr") or defaults.sum_assured_inr),
        term_years=values.get("term_years") or defaults.term_years,
        ppt=defaults.ppt,
        rider_uins=defaults.rider_uins,
        age_years=values["age_years"],
        gender=values.get("gender"),
        tobacco_12m=values["tobacco_12m"],
        frequency=defaults.frequency,
    )
    try:
        quote = await turn.domain.create_quote(request)
    except DomainError as exc:
        if exc.code == "QUOTE_OUT_OF_BOUNDS" and exc.problem is not None:
            _bounds(screen, product, exc.problem)
        elif exc.code == "PRODUCT_WITHDRAWN":
            _unavailable(
                screen, ("plan_unavailable", texts.plan_unavailable.format(plan=product.name))
            )
        else:
            raise
        return
    append(
        turn,
        EventType.ENGINE_DECISION,
        EngineDecisionHeader(
            service="quote",
            decision_id=str(quote.decision_id),
            rules_version=current.pins.rules,
            inputs_sha256=quote.inputs_sha256,
            reason_codes=quote.reason_codes,
        ),
        {"request": request.model_dump(mode="json"), "result": quote.model_dump(mode="json")},
    )
    current.quote = quote
    disclosure = await turn.domain.get_disclosure(QUOTE_02, current.locale)
    turn.shown.append(disclosure.body)
    labels = texts.labels
    card = texts.quote_card.format(
        plan=product.name,
        uin=uin,
        premium=format_inr(quote.annual_premium_inr),
        period=texts.screening.period[quote.ppt],
        cover=format_inr(quote.sum_assured_inr),
        term=quote.term_years,
        ppt=labels.ppt[quote.ppt],
        valid_until=_date(quote.valid_until),
    )
    logger.info("quote %s shown: %s", quote.quote_id, quote.rating_version)
    _next_step(
        screen,
        ("quote_card", card),
        (f"registry:{QUOTE_02}", disclosure.body),
        ("quote_caveat", texts.quote_caveat),
        ("quote_next", texts.quote_next),
    )


def _next_step(screen: Screen, *parts: tuple[str, str]) -> None:
    """After a quote (or none, with tobacco declined): a full needs check, or done."""
    sc = scripts(screen.turn).screening
    screen.session.pending_slot = None
    screen.session.last_prompt_id = QUOTED
    screen.turn.phrase = None
    screen.reply(
        list(parts),
        [quick_reply(sc.opt_in, "OPT_IN", {}), quick_reply(sc.satisfied, "SATISFIED", {})],
    )


def _bounds(screen: Screen, product: Product, problem: Problem) -> None:
    """422 QUOTE_OUT_OF_BOUNDS: the member named in `field`, re-asked with the problem's own bounds,
    never numbers of the model's. A stated value refused is cleared (a declined row), so the next
    quote takes the defaults unless the customer states another. An entry age outside the band is
    not re-asked: another plan is."""
    texts = scripts(screen.turn)
    sc = texts.screening
    field, low, high = problem.field, problem.allowed_min, problem.allowed_max
    logger.info("quote out of bounds on %s", field)
    if field == "sum_assured_inr" and low is not None:
        span = (
            sc.range.format(min=format_inr(low), max=format_inr(high))
            if high is not None
            else texts.labels.or_more.format(amount=format_inr(low))
        )
        step = format_inr(problem.allowed_step) if problem.allowed_step else format_inr(low)
        part = ("cover_bounds", texts.cover_bounds.format(plan=product.name, range=span, step=step))
    elif field == "term_years" and low is not None and high is not None:
        span = sc.range.format(min=int(float(low)), max=int(float(high)))
        part = ("term_bounds", texts.term_bounds.format(plan=product.name, range=span))
    elif field == "age_years" and low is not None and high is not None:
        span = sc.range.format(min=int(float(low)), max=int(float(high)))
        _unavailable(screen, ("age_bounds", texts.age_bounds.format(plan=product.name, range=span)))
        return
    else:  # a member the customer never set (PPT, riders): the plan cannot be priced here
        _unavailable(screen, ("plan_unavailable", texts.plan_unavailable.format(plan=product.name)))
        return
    if field in screen.known:
        screen.write(field, None, "declined", 1.0)
    screen.session.pending_slot = field
    screen.session.last_prompt_id = f"qo.ask:{field}"  # the answer is read like any question's
    screen.turn.phrase = None
    screen.reply([part], [])


def _unavailable(screen: Screen, part: tuple[str, str]) -> None:
    """The plan can't be priced (withdrawn, not launched, unknown, outside its entry ages): ask for
    another. I3: never a list of others."""
    current = screen.session
    current.focus_uins, current.quote = [], None
    current.pending_slot, current.last_prompt_id = None, PLAN
    screen.turn.phrase = None
    screen.reply([part], [])


def _hard_block(turn: Any, current: SessionState) -> None:
    """QO.1 (C13): no application without suitability; the needs check is offered."""
    sc = scripts(turn).screening
    turn.signals["apply_request"] = True
    current.last_prompt_id = BLOCKED
    turn.parts = [("hard_block", scripts(turn).hard_block)]
    turn.quick_replies = [
        quick_reply(sc.opt_in, "OPT_IN", {}),
        quick_reply(sc.satisfied, "SATISFIED", {}),
    ]
    logger.info("application asked for in Quote-Only: hard block (C13)")


def _opt_in(turn: Any, current: SessionState) -> None:
    """QO.3 / QO.3b: S2 when eligibility is established, else S1 first (its enter asks next)."""
    turn.signals["advisory_opt_in"] = True
    engine = current.eligibility.engine if current.eligibility else None
    if engine is not None and engine.outcome == "ELIGIBLE":
        turn.parts = [("screening_done", scripts(turn).screening_done)]
        turn.quick_replies = []
    logger.info("Quote-Only opt-in to full advice")


async def _satisfied(turn: Any, current: SessionState) -> None:
    """QO.2: a summary; the re-engagement line only with P3 (marketing) granted."""
    texts = scripts(turn)
    turn.signals["quote_satisfied"] = True
    quote = current.quote
    if quote is None:
        parts = [("goodbye", texts.goodbye)]
    else:
        names = await product_names(turn)
        parts = [
            (
                "quote_summary",
                texts.quote_summary.format(
                    plan=names.get(quote.uin, quote.uin),
                    uin=quote.uin,
                    premium=format_inr(quote.annual_premium_inr),
                    period=texts.screening.period[quote.ppt],
                    valid_until=_date(quote.valid_until),
                ),
            )
        ]
    if current.consent is not None and "P3" in granted(current.consent.purposes):
        parts.append(("reengage", texts.reengage))
    turn.parts, turn.quick_replies = parts, []


def _date(day: date) -> str:
    return f"{day.day} {day:%B %Y}"
