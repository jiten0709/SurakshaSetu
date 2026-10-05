"""Pause (row CC3, V3, and S3.2) and resume (row PAUSE.R; TDD §3.9).

Entering PAUSE (`pause`, after decide): the paused template. decide has already pushed the frame,
and commit sets conv.session.status to "paused".

A customer turn in PAUSE runs `resume` (the PAUSE state's node) before decide. It revalidates what
may have moved while the customer was away:
- the consent record, fetched fresh. A lapsed TTL or a superseded notice makes P1 invalid, so the
  G1 guard re-enters S0 once PAUSE.R resumes;
- the products and quotes of a recommendation (a kill switch, a withdrawal, a quote past its IST
  validity). A stale recommendation is dropped, so S3 ranks and quotes again (Step 21);
- the pins change only by kill switch, and load has already re-pinned a kill-switched bundle.
It is not wrapped by `guarded`: if the Consent Service cannot answer, the turn fails (503, nothing
released) and does not resume on stale consent.
"""

import logging
from typing import Any
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime

from surakshasetu.domain.client import DomainError
from surakshasetu.graph import handlers
from surakshasetu.graph.handlers import scripts, session
from surakshasetu.graph.state import GraphState, RecommendationPayload

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")


async def pause(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    texts = scripts(turn)
    # Step 20: a dependency down (CC3) says why before the saved-progress line. Step 21: what the
    # state node prepared follows it (S3.2's summary of the options shown).
    down = bool(turn.signals.get("dependency_down"))
    why = [("dependency_down", texts.dependency_down)] if down else []
    turn.parts = [*why, ("paused", texts.paused), *turn.parts]
    logger.info("session paused%s", " (dependency down)" if why else "")


async def resume(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    current = session(turn)
    if current.consent is not None:
        current.consent = await turn.domain.get_consent_record(current.consent.consent_id)
    if current.recommendation is not None:
        stale = await _stale(turn, current.recommendation)
        if stale is not None:
            current.recommendation = None
            logger.info("recommendation dropped on resume: %s", stale)
    valid = "valid" if _valid(current.consent) else "not valid"
    logger.info("resume revalidated: consent %s", valid)


def _valid(consent: Any) -> bool:
    return consent is not None and bool(consent.valid_p1)


async def _stale(turn: Any, recommendation: RecommendationPayload) -> str | None:
    """Also S3's revalidation on every turn (Step 21): why the recommendation shown is out of date,
    or None. A product that can no longer be sold comes first (S3 ranks again), then an expired
    quote (S3 quotes it again)."""
    now = handlers.now()
    today = now.astimezone(IST).date()
    uins = [option.uin for option in recommendation.options]
    if any(("product", uin) in turn.kill_switches for uin in uins):
        return "PRODUCT_KILL_SWITCH"
    for uin in uins:
        try:
            product = await turn.domain.get_product(uin, as_of=now)
        except DomainError as exc:
            if exc.code != "NOT_FOUND":  # a withdrawn product's catalog row ends with its sale
                raise
            return "PRODUCT_WITHDRAWN"
        if product.status != "in_force" or (
            product.effective_to is not None and product.effective_to < today
        ):
            return "PRODUCT_WITHDRAWN"
    if any(o.quote is not None and o.quote.valid_until < today for o in recommendation.options):
        return "QUOTE_EXPIRED"
    return None
