"""The objection handler: TDD §3.9's row CC5 (Step 22).

It answers only with cited facts and the engine's alternatives, and never argues (TDD §3.9; the
architecture spec §5.1):
- price: in S3, the engine's lower-cost versions of the top option, each with its protection gap
  (S3's alternatives); before any premium exists, when one will be shown, with no figure;
- trust: the claim process from the corpus, cited (the side-query machinery, on the claims topic);
- competitor: no comparison; the insurer's own plans only;
- guarantee: no promise; with plans shown or a plan named, what the policy wording defines, cited;
- anything else ("insurance is a waste"): an acknowledgment that leaves the decision with them;
- a deferral or "I need to ask my spouse": the Pause row (S3.2 in S3; CC3b in S1, Quote-Only, S2).
The same objection a second time (SS_OBJECTION_REPEAT_LIMIT) gets no rebuttal: the offer to save
and come back (Pause), to end the conversation (CC5b, Exit), or to carry on. The RESPONSE_RELEASED
header records the objection and how it was answered.

Like the side-query subgraph, the origin state's node runs first (graph/nodes.objection) and puts
its pending prompt again; the answer leads the reply. A deferral and the END action skip it: the
pause handler or the exit line speaks.
"""

import logging
from typing import Any, Literal

from surakshasetu.analysis.models import Intent
from surakshasetu.compose.bundle import mentions
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import side_query
from surakshasetu.graph.handlers import bundle, granted, quick_reply, scripts, session
from surakshasetu.graph.states import s3

logger = logging.getLogger(__name__)

Kind = Literal["price", "trust", "competitor", "guarantee", "other", "deferral", "end"]
# The objection intents, most specific first: a turn with two is answered as the first.
TYPES: dict[Intent, Kind] = {
    Intent.OBJECTION_PRICE: "price",
    Intent.OBJECTION_GUARANTEE: "guarantee",
    Intent.OBJECTION_COMPETITOR: "competitor",
    Intent.OBJECTION_TRUST: "trust",
    Intent.OBJECTION_OTHER: "other",
}
ENGAGED = frozenset({FsmState.S1, FsmState.QUOTE_ONLY, FsmState.S2, FsmState.S3})


def detect(turn: Any) -> Kind | None:
    """The objection this turn raises: the END quick reply, SAVE or a deferral outside S3 (S3's own
    rows handle both there), an objection intent, or the bundle's phrases (guarantee and competitor
    from needs.yaml, "cheaper" from s3.yaml). Only after consent, in S1, Quote-Only, S2 and S3; S0
    answers privacy questions only."""
    current = session(turn)
    state = current.fsm_state
    if state not in ENGAGED:
        return None
    kind = (turn.action or {}).get("type")
    if kind == "END":
        return "end"
    if kind == "SAVE":
        return "deferral" if state is not FsmState.S3 else None
    pipeline = turn.pipeline
    if turn.text is None or pipeline is None or pipeline.blocked:
        return None
    intents = set(pipeline.analysis.intents) if pipeline.analysis else set()
    text = pipeline.stored_raw
    lexicons = bundle(turn)
    if state is not FsmState.S3 and (
        Intent.NEED_TIME in intents or mentions(lexicons.s3_lexicon.need_time, text)
    ):
        return "deferral"
    for intent, name in TYPES.items():
        if intent in intents:
            return name
    if mentions(lexicons.s3_lexicon.cheaper, text):
        return "price"
    if mentions(lexicons.needs_lexicon.guarantee, text):
        return "guarantee"
    if mentions(lexicons.needs_lexicon.competitor, text):
        return "competitor"
    return None


async def respond(turn: Any, kind: Kind) -> None:
    current = session(turn)
    texts = scripts(turn)
    turn.objection = kind
    p3 = current.consent is not None and "P3" in granted(current.consent.purposes)
    quoted = current.fsm_state in (FsmState.QUOTE_ONLY, FsmState.S3)  # the line names a quote
    reengage = [("reengage", texts.reengage)] if p3 and quoted else []
    if kind == "end":
        turn.signals["end_requested"] = True  # CC5b: Exit
        _reply(turn, [("declined_exit", texts.declined_exit), *reengage], [])
        turn.objection_response = "exit"
        logger.info("conversation ended after a repeated objection")
        return
    if kind == "deferral":
        # CC3b (S1, Quote-Only, S2): the pause handler leads with the saved-progress line. Never an
        # objection row: a deferral that also reads as an objection must still pause.
        turn.signals["need_time"] = True
        turn.signals["objection"] = False
        _reply(turn, reengage, [])
        turn.objection_response = "pause"
        logger.info("deferral outside S3: paused")
        return
    count = current.counters.get(f"objection_{kind}", 0) + 1
    current.counters = {**current.counters, f"objection_{kind}": count}
    if count >= turn.settings.objection_repeat_limit:
        side = texts.side
        _reply(
            turn,
            [("objection_repeat", texts.objection_repeat)],
            [
                quick_reply(side.save, "SAVE", {}),
                quick_reply(side.end, "END", {}),
                quick_reply(side.keep_going, "CONTINUE", {}),
            ],
        )
        turn.objection_response = "offer_pause_exit"
        logger.info("the same objection again (%s): pause or exit offered", kind)
        return
    if turn.render is not None or turn.degraded:
        turn.objection_response = "state"  # the options were presented again, or the tier is down
        return
    question = _question(turn)
    if kind == "price":
        rec = current.recommendation
        if current.fsm_state is FsmState.S3 and rec is not None:
            await s3.cheaper(turn, rec, rec.options[0].uin)
            turn.objection_response = "alternatives"
        else:
            _lead(turn, ("objection_price_early", texts.objection_price_early))
            turn.objection_response = "no_figure"
    elif kind == "trust":
        await side_query.respond(turn, question, entities=("claims",), counted=False)
        turn.objection_response = f"cited:{turn.side_outcome}"
    elif kind == "competitor":
        insurer = bundle(turn).manifest.insurer
        _lead(turn, ("competitor_note", texts.competitor_note.format(insurer=insurer)))
        turn.objection_response = "declined"
    elif kind == "guarantee":
        note = ("guarantee_note", texts.guarantee_note)
        shown = current.fsm_state is FsmState.S3 and current.recommendation is not None
        if shown or current.focus_uins:
            await side_query.respond(turn, question, lead=(note,), counted=False)
            turn.objection_response = f"wording:{turn.side_outcome}"
        else:
            _lead(turn, note)
            turn.objection_response = "no_promise"
    else:
        _lead(turn, ("objection_other", texts.objection_other))
        turn.objection_response = "acknowledged"
    logger.info("objection %s answered: %s", kind, turn.objection_response)


def _question(turn: Any) -> str:
    pipeline = turn.pipeline
    analysis = pipeline.analysis if pipeline is not None else None
    if analysis is not None and analysis.side_query:
        return str(analysis.side_query)
    return pipeline.stored_raw if pipeline is not None else ""


def _lead(turn: Any, part: tuple[str, str]) -> None:
    """A template answer before the state's own prompt (no generated sentence with it)."""
    turn.parts = [part, *turn.parts]
    turn.phrase = None


def _reply(turn: Any, parts: list[tuple[str, str]], quick: list[dict[str, Any]]) -> None:
    turn.parts, turn.quick_replies, turn.form, turn.phrase = parts, quick, None, None
