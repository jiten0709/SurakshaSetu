"""The S3 node: State-3, recommendation, comparison and disclosure (TDD §3.8; Step 21).

Entering S3 (`enter`, after S2.2 or a resume) presents the recommendation, in TDD §3.8's sequence:
1. POST /v1/ranking/rank under the pinned rules: the eligible UINs and flags from S1, the bound
   suitability record (I2: the ranking must echo its inputs hash), the life assured's age,
   tobacco, the channel and pinned language, and the kill-switched products left out (the
   contract's excluded_uins; a rejected plan is ranked again after re-discovery, decided
   2026-10-05). The ranker prices each option through the quote adapter; a quote is null with
   PREMIUM_WITHHELD or RATING_UNAVAILABLE. ENGINE_DECISION records the request as sent.
2. Per option, the catalog product (its CIS and policy wording) and the registry's disclosure set
   for the channel and pinned language.
3. Evidence for the option UINs (product and regulatory), RETRIEVAL audited.
4. gen-recommend with the envelope: the options and the needs assessment as [R#] engine facts (no
   money), the evidence as [E#], the confirmed needs as session facts. Numbers only as
   placeholders, which the composer fills from the ranking.
5. The output rails (graph/nodes.validate): one regeneration with the error list, then the
   deterministic card; the disclosure sets are hash-checked before release (I4).
6. The commit records conv.recommendation (the options, the ranker version, the suitability inputs
   hash, the released text's hash) with what each option's render showed: the registry set and
   the document hashes an acknowledgment must match. The whole reply is released at once.

A turn in S3 (`node`): the V4 hooks first, then the rows (save or need time -> Pause with the
options kept in the session; all rejected -> S2 with the re-discovery framing; a decline), then the
revalidation every turn (a product withdrawn or kill-switched ranks again; an expired quote is
quoted again with its own values), then the customer's choice:
- APPLY {uin, and optionally cover, term, PPT or riders}: the option's quote, or POST /v1/quotes for
  exactly the customer's choice (the protection gap then theirs), and the acknowledgment asked;
- CHEAPER {uin}: the engine's alternatives, each with its protection gap;
- DISCLOSURE_ACK {uin, registry_version, disclosure_set_sha256, document_sha256}: checked against
  the registry and what this render showed; a mismatch presents the options again. With a plan
  chosen, its quote valid and every acknowledgment for it matching (V7), the signed intake goes to
  the application journey (handoff/intake.py) and S3.4 hands off; the journey down keeps S3, saves
  the intake for a retry, and an advisor completes it.
Free text: "which one should I buy?" (the top option and its reasons; the choice stays the
customer's), a plan that is not an option (why, from the held eligibility and suitability, and an
advisor), "make it cheaper", and questions (tax, an exclusion dispute, a guarantee) answered from
the evidence with citations, the state's caveat, and for tax DISC-GLOBAL-TAX-05 verbatim. Anything
else gets structured choices (TDD §3.9).

The transition for an entering turn is already decided, so a problem found there is answered on
that turn and moved on the next: no options -> a careful line, and the next S3 turn escalates
(S3.0, HE_NO_OPTION); the ranker down -> a retry, and a second outage pauses (CC3); a registry that
fails its own integrity check -> the release-blocked template and an advisor.
"""

import asyncio
import hashlib
import logging
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

from langgraph.runtime import Runtime

from surakshasetu.analysis.models import Intent
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.events import (
    DisclosureAckHeader,
    EngineDecisionHeader,
    EventType,
    HandoffHeader,
    RetrievalHeader,
)
from surakshasetu.compose import composer
from surakshasetu.compose.bundle import mentions
from surakshasetu.compose.citations import EngineFact, TurnHandles, issue, render, source_list
from surakshasetu.compose.composer import Rendered
from surakshasetu.compose.envelope import EnvelopeError, SessionFacts, model_call_event
from surakshasetu.compose.envelope import build as build_envelope
from surakshasetu.compose.placeholders import format_date, format_inr
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.domain.client import DomainError
from surakshasetu.domain.models import (
    DisclosureSet,
    EligibilityResult,
    NeedsPayload,
    Pins,
    PremiumQuote,
    Product,
    QuoteAlternativesRequest,
    QuoteRequest,
    RankingRequest,
    RankingResult,
    RecommendedOption,
    SuitabilityResult,
)
from surakshasetu.gateway import GatewayUnavailable
from surakshasetu.graph import handlers
from surakshasetu.graph.handlers import (
    append,
    bundle,
    granted,
    human_escalation,
    pause,
    product_names,
    quick_reply,
    scripts,
    session,
)
from surakshasetu.graph.state import (
    DisclosureAck,
    EligibilityPayload,
    GraphState,
    RecommendationPayload,
    Selection,
    SessionState,
    Shown,
)
from surakshasetu.graph.states import s1, s2
from surakshasetu.graph.states.quote_only import bounds_part
from surakshasetu.graph.states.s1 import QUESTIONS, valid
from surakshasetu.handoff import intake
from surakshasetu.rails.normalise import normalise
from surakshasetu.rails.output import Numbers, products_named
from surakshasetu.retrieval.service import RetrievalContext, RetrievalResult, RetrievalUnavailable
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
LANGUAGE: dict[str, Literal["en", "hi"]] = {"en-IN": "en", "hi-IN": "hi"}
# S3's own evidence query: the options' benefits and exclusions (routing.yaml's RT-EXCLUSION:
# product and regulatory, with at least one product chunk), filtered to the option UINs.
EVIDENCE_QUERY = "key benefits, exclusions and waiting periods of the plan"
TAX_05 = "DISC-GLOBAL-TAX-05"
_TAX = re.compile(r"(?i)\btax(es)?\b|टैक्स")
OPTIONS, CHOOSE = "s3.options", "s3.choose"
CHOICE_KEYS = ("sum_assured_inr", "term_years", "ppt", "rider_uins")


class Stale(Exception):
    """The inputs S3 needs are no longer bound (I2) or no longer eligible: nothing is presented."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# --- small readers --------------------------------------------------------------------------------
def _today(turn: Any) -> Any:
    return handlers.now().astimezone(IST).date()


def quote_valid(quote: PremiumQuote | None, today: Any) -> bool:
    """V7: a quote exists and the IST date is not past its validity."""
    return quote is not None and today <= quote.valid_until


def check_ack(ack: Mapping[str, Any], shown: Shown, registry: DisclosureSet) -> list[str]:
    """An acknowledgment against what this render showed and what the registry holds now. Empty
    when it binds: the shown set (version and hash) is still the registry's, and the document
    hashes are exactly those shown (CIS and policy wording, and BI where one was shown)."""
    problems = []
    if ack.get("registry_version") != shown.registry_version:
        problems.append("REGISTRY_VERSION_NOT_SHOWN")
    if ack.get("disclosure_set_sha256") != shown.set_sha256:
        problems.append("SET_NOT_SHOWN")
    if (registry.registry_version, registry.set_sha256) != (
        shown.registry_version,
        shown.set_sha256,
    ):
        problems.append("REGISTRY_MOVED")
    if ack.get("document_sha256") != shown.documents:
        problems.append("DOCUMENTS_NOT_SHOWN")
    return problems


def acks_valid(rec: RecommendationPayload, uin: str) -> bool:
    """V7: at least one acknowledgment for the UIN, and every one matches what was shown."""
    shown = rec.shown.get(uin)
    acks = [a for a in rec.acks if a.uin == uin]
    return (
        shown is not None
        and bool(acks)
        and all(
            (a.registry_version, a.disclosure_set_sha256, a.document_sha256)
            == (shown.registry_version, shown.set_sha256, shown.documents)
            for a in acks
        )
    )


def _language(turn: Any) -> Literal["en", "hi", "hi-Latn"]:
    if turn.pipeline is not None:
        return cast(Literal["en", "hi", "hi-Latn"], turn.pipeline.language)
    return LANGUAGE[session(turn).locale]


def _slots(turn: Any) -> dict[str, Any]:
    row = cast(SessionRow, turn.row)
    return store.current_slots(turn.conn, turn.keys, row.key_ref, turn.session_id)


def _life_assured_age(current: SessionState, slots: dict[str, Any]) -> int:
    """Rank and price on the life assured's profile (Step 20 C5)."""
    eligibility = cast(EligibilityPayload, current.eligibility)
    la = slots.get("proposer.la_age") if slots.get("proposer.is_life_assured") is False else None
    return la if isinstance(la, int) else eligibility.age_years


def _period(turn: Any, quote: PremiumQuote) -> str:
    return scripts(turn).screening.period[quote.ppt]


def _cta(turn: Any) -> list[dict[str, Any]]:
    """TDD §3.8: four equal choices, in a fixed order, no default."""
    cta = bundle(turn).templates[session(turn).locale].recommendation.cta
    return [
        quick_reply(cta.apply, "APPLY", {}),
        quick_reply(cta.advisor, "HUMAN_REQUEST", {}),
        quick_reply(cta.revise, "REVISE", {}),
        quick_reply(cta.save, "SAVE", {}),
    ]


def _reply(turn: Any, parts: list[tuple[str, str]], quick: list[dict[str, Any]]) -> None:
    turn.parts, turn.quick_replies, turn.form, turn.phrase = parts, quick, None, None


def _advisor(turn: Any) -> dict[str, Any]:
    return quick_reply(scripts(turn).screening.advisor, "HUMAN_REQUEST", {})


def _record(
    turn: Any,
    service: str,
    decision_id: str,
    inputs_sha256: str,
    reason_codes: list[str],
    payload: dict[str, Any],
) -> None:
    append(
        turn,
        EventType.ENGINE_DECISION,
        EngineDecisionHeader(
            service=service,
            decision_id=decision_id,
            rules_version=session(turn).pins.rules,
            inputs_sha256=inputs_sha256,
            reason_codes=reason_codes,
        ),
        payload,
    )


# --- the recommendation ---------------------------------------------------------------------------
async def _rank(turn: Any, slots: dict[str, Any]) -> RankingResult:
    current, row = session(turn), cast(SessionRow, turn.row)
    lost = current.eligibility is None or current.eligibility.engine is None
    if lost and not await s1.recompute(turn):  # hydration lost it, and it no longer holds
        raise Stale("ELIGIBILITY")
    eligibility = cast(EligibilityPayload, current.eligibility)
    engine = cast(EligibilityResult, eligibility.engine)
    needs, suitability = current.needs, current.suitability
    if needs is None or suitability is None or needs.slots_sha256 != suitability.inputs_sha256:
        raise Stale("SUITABILITY_NOT_BOUND")
    request = RankingRequest(
        pins=Pins(rules=current.pins.rules),
        eligible_uins=engine.eligible_uins,
        suitability=suitability,
        excluded_uins=sorted(target for kind, target in turn.kill_switches if kind == "product"),
        channel=row.channel,
        language=current.locale,
        as_of=handlers.now(),
        tobacco_12m=eligibility.tobacco_12m,
        gender=eligibility.gender,
        age_years=_life_assured_age(current, slots),
        flags=engine.flags,
    )
    result: RankingResult = await turn.domain.rank_options(request)
    _record(
        turn,
        "ranking",
        str(result.decision_id),
        result.inputs_sha256,
        result.reason_codes,
        {
            "request": request.model_dump(mode="json", exclude_unset=True),
            "result": result.model_dump(mode="json"),
        },
    )
    logger.info(
        "ranking %s: %d options %s",
        result.decision_id,
        len(result.options),
        [o.uin for o in result.options],
    )
    if result.suitability_inputs_sha256 != needs.slots_sha256:
        logger.error("I2: the ranking is not for the bound suitability record; nothing shown")
        raise Stale("I2_MISMATCH")
    return result


async def _retrieve(
    turn: Any,
    query: str,
    uins: list[str],
    *,
    entities: list[str] | None = None,
    intents: list[Intent] | None = None,
) -> RetrievalResult | None:
    """Evidence for the options, RETRIEVAL audited (its payload holds the queries, which may be
    the customer's words). None when retrieval is not available: the reply cites the engine only."""
    if turn.retrieval is None:
        return None
    current = session(turn)
    context = RetrievalContext(
        fsm_state="S3",
        intents=intents or [],
        entities=entities or [],
        focus_uins=uins,
        language=_language(turn),
        as_of=handlers.now(),
        corpus_pins=cast(Any, current.pins.corpus),
    )
    try:
        result: RetrievalResult = await turn.retrieval.retrieve(query, context)
    except RetrievalUnavailable as exc:
        logger.warning("retrieval unavailable in S3: %s; the engine facts alone", exc.reason)
        return None
    audit = result.audit
    append(
        turn,
        EventType.RETRIEVAL,
        RetrievalHeader(
            collections=list(audit.collections),
            snapshot_ids=audit.snapshot_ids,
            chunk_ids=audit.chunk_ids,
            rerank_scores=[s for s in audit.rerank_scores if s is not None],
            abstained=result.abstained,
        ),
        audit.model_dump(mode="json"),
    )
    return result


def _engine_facts(
    turn: Any, options: list[RecommendedOption], names: Mapping[str, str]
) -> list[EngineFact]:
    """What the model may cite as [R#]: each option's ranking and the needs assessment. Never a
    premium or cover amount: the model writes those as placeholders."""
    labels = scripts(turn).s3
    suitability = cast(SuitabilityResult, session(turn).suitability)
    facts = [
        EngineFact(
            rule=o.reason_codes[0] if o.reason_codes else "RANKED",
            label=labels.engine_option.format(name=names.get(o.uin, o.uin), uin=o.uin),
            content={
                "uin": o.uin,
                "rank": o.rank,
                "term_years": o.term_years,
                "ppt_years": o.ppt_years,
                "rider_uins": o.rider_uins,
                "reason_codes": o.reason_codes,
            },
        )
        for o in options
    ]
    facts.append(
        EngineFact(
            rule=",".join(suitability.rule_ids) or "SUITABILITY",
            label=labels.engine_needs,
            content={
                "fit_types": suitability.fit_types,
                "affordability": suitability.affordability,
                "cover_to_age": suitability.assumptions.cover_to_age,
                "dependency_years": suitability.assumptions.dependency_years,
                "reason_codes": suitability.reason_codes,
            },
        )
    )
    return facts


def _session_facts(turn: Any) -> SessionFacts:
    """The confirmed needs, as a REDACTED route may know them: no number, no identifier."""
    needs = session(turn).needs
    if needs is None:
        return SessionFacts(language=_language(turn))
    return SessionFacts(
        language=_language(turn),
        answered_slots=[s for s in s2.NEEDS if getattr(needs, s, None) is not None],
        goals=needs.goals or [],
        income_type=needs.income_type,
        dependant_relations=sorted({d.relation for d in needs.dependants or []}),
        liability_kinds=sorted({li.kind for li in needs.liabilities or []}),
    )


async def _generate(
    turn: Any,
    l1: Literal["S3", "side-query"],
    retrieval: RetrievalResult | None,
    facts: list[EngineFact],
    errors: list[str] | None = None,
) -> tuple[str | None, TurnHandles]:
    """One gen-recommend draft (MODEL_CALL appended) and the handles its envelope carried. None
    when the envelope is refused or the route is down: the deterministic card or the template."""
    current = session(turn)
    evidence = retrieval.evidence if retrieval else []
    try:
        envelope = build_envelope(
            bundle(turn),
            l1=l1,
            locale=current.locale,
            user_text=turn.pipeline.redacted if turn.pipeline else "",
            facts=_session_facts(turn),
            retrieval=retrieval,
            engine=facts,
            corrections=errors or (),
        )
    except EnvelopeError as exc:
        logger.warning("%s envelope refused: %s; no generation", l1, exc.reason)
        return None, issue(evidence, facts)
    try:
        result = await turn.gateway.call(
            envelope.route,
            data_class=envelope.data_class,
            messages=envelope.messages,
            session_id=turn.session_id,
            turn_id=turn.out_id,
            fsm_state=current.fsm_state.value,
            attestation=envelope.attestation,
        )
    except GatewayUnavailable as exc:
        logger.warning("%s generation unavailable: %s", l1, exc.reason)
        return None, envelope.handles
    header, payload = model_call_event(envelope, result)
    append(turn, EventType.MODEL_CALL, header, payload)
    return result.content, envelope.handles


def _with_lead(lead: list[tuple[str, str]], rendered: Rendered) -> Rendered:
    """Template parts (the S2 bridge, a note) before the composed recommendation, hashed as one."""
    if not lead:
        return rendered
    named = [(i if ":" in i else f"template:{i}", t) for i, t in lead]
    text = "\n\n".join([*(t for _, t in named), rendered.text])
    return Rendered(
        text,
        sha256_hex_text(text),
        [*named, *rendered.parts],
        rendered.citations,
        rendered.sources,
        rendered.disclosure_hashes,
        rendered.documents_shown,
    )


def sha256_hex_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def present(
    turn: Any, lead: list[tuple[str, str]], ranking: RankingResult | None = None
) -> bool:
    """The recommendation (TDD §3.8's sequence): ranked (or the given ranking, re-quoted), its
    catalog and registry parts, evidence, gen-recommend, and the composer as the render the rails
    check. False when the ranker found no option. A DomainError or Stale propagates."""
    current = session(turn)
    current.recommendation = None  # superseded; the commit records the new one once released
    if ranking is None:
        ranking = await _rank(turn, _slots(turn))
    if not ranking.options:
        return False
    uins = [o.uin for o in ranking.options]
    row, as_of = cast(SessionRow, turn.row), handlers.now()
    products: list[Product] = await asyncio.gather(
        *(turn.domain.get_product(u, as_of=as_of) for u in uins)
    )
    sets: list[DisclosureSet] = await asyncio.gather(
        *(turn.domain.get_disclosure_set(u, row.channel, current.locale, as_of=as_of) for u in uins)
    )
    by_uin = {p.uin: p for p in products}
    set_by_uin = {s.uin: s for s in sets}
    retrieval = await _retrieve(turn, EVIDENCE_QUERY, uins, entities=["exclusion"])
    facts = _engine_facts(turn, ranking.options, {u: p.name for u, p in by_uin.items()})
    draft, handles = await _generate(turn, "S3", retrieval, facts)
    suitability = cast(SuitabilityResult, current.suitability)
    needs = cast(NeedsPayload, current.needs)
    partial = suitability.profile_sufficiency < turn.settings.profile_sufficiency_min
    shown_ranking = ranking

    def render_(narrative: str | None) -> Rendered:
        return _with_lead(
            lead,
            composer.compose(
                bundle(turn),
                locale=current.locale,
                needs=needs,
                suitability=suitability,
                ranking=shown_ranking,
                products=by_uin,
                disclosure_sets=set_by_uin,
                handles=handles,
                narrative=narrative,
                partial_profile=partial,
            ),
        )

    async def regenerate(errors: list[str]) -> str | None:
        return (await _generate(turn, "S3", retrieval, facts, errors))[0]

    turn.draft, turn.render, turn.regenerate = draft, render_, regenerate
    turn.handles, turn.numbers = handles, Numbers(ranking, suitability)
    turn.disclosure_sets = set_by_uin
    turn.recommendation = RecommendationPayload(
        options=ranking.options,
        ranker_version=ranking.ranker_version,
        suitability_inputs_sha256=ranking.suitability_inputs_sha256,  # == slots_sha256 (I2)
        evidence_map={},  # set at the commit: the handles the released text cites
        rendered_sha256="",  # set at the commit: the text released
        ranking=ranking,
    )
    current.pending_slot, current.last_prompt_id = None, OPTIONS
    turn.parts, turn.phrase, turn.form = [], None, None
    turn.quick_replies = _cta(turn)
    turn.products = turn.products or await product_names(turn)
    return True


# --- entering S3 ----------------------------------------------------------------------------------
async def enter(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """S2.2 (or a resume) entered S3: the recommendation after the bridge."""
    turn = runtime.context
    current = session(turn)
    lead = list(turn.parts)
    try:
        if not await present(turn, lead):
            _reply(
                turn,
                [*lead, ("no_option", scripts(turn).no_option)],
                [quick_reply(scripts(turn).screening.advisor, "CONTINUE", {})],
            )
            logger.info("S3 entered with no option: escalated next turn (HE_NO_OPTION)")
    except (DomainError, Stale) as exc:
        _entry_failed(turn, lead, exc)
    logger.info("S3 entered: %s", current.last_prompt_id)


def _entry_failed(turn: Any, lead: list[tuple[str, str]], exc: DomainError | Stale) -> None:
    texts = scripts(turn)
    if isinstance(exc, DomainError) and exc.code == "REGISTRY_INTEGRITY":
        _blocked(turn, lead)
        return
    if isinstance(exc, DomainError) and exc.status is not None and exc.status < 500:
        raise exc
    reason = exc.reason if isinstance(exc, Stale) else exc.code
    logger.warning("recommendation unavailable entering S3: %s; retry offered", reason)
    _clear(turn)
    _reply(
        turn,
        [*lead, ("rec_retry", texts.rec_retry)],
        [quick_reply(texts.screening.retry, "RETRY", {})],
    )


def _blocked(turn: Any, lead: list[tuple[str, str]]) -> None:
    """The registry failed its own integrity check: no product is shown without its verified
    disclosure set (I4)."""
    texts = scripts(turn)
    logger.error("disclosure registry integrity failure: no option shown (I4)")
    _clear(turn)
    _reply(
        turn,
        [*lead, ("release_blocked", texts.release_blocked), ("advisor_offer", texts.advisor_offer)],
        [_advisor(turn)],
    )


def _clear(turn: Any) -> None:
    turn.draft, turn.render, turn.regenerate = None, None, None
    turn.handles, turn.numbers, turn.disclosure_sets, turn.recommendation = None, None, {}, None


def _down(turn: Any, exc: DomainError | Stale) -> None:
    """In a turn already in S3: a registry integrity failure blocks; an unbound record or a lost
    eligibility goes back through the rows; a domain outage pauses (TDD §3.9: save and resume)."""
    if isinstance(exc, DomainError) and exc.code == "REGISTRY_INTEGRITY":
        _blocked(turn, [])
        return
    if isinstance(exc, DomainError) and exc.status is not None and exc.status < 500:
        raise exc
    _clear(turn)
    if isinstance(exc, Stale) and exc.reason == "ELIGIBILITY":
        turn.signals["correction"] = "eligibility"  # G2: S1's rows decide again
        return
    reason = exc.reason if isinstance(exc, Stale) else exc.code
    logger.warning("S3 dependency unavailable: %s; pausing", reason)
    turn.signals["dependency_down"] = True
    turn.parts, turn.quick_replies = [], []


# --- a turn in S3 ---------------------------------------------------------------------------------
async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    current = session(turn)
    await s1.correction(turn)  # V4: an eligibility fact corrected in S3 re-runs S1's rows (G2)
    await s2.correction(turn)  # ... a needs fact, S2's (G3)
    action = turn.action or {}
    kind, payload = action.get("type"), action.get("payload") or {}
    if turn.signals.get("correction") or not valid(current) or kind == "HUMAN_REQUEST":
        return  # G2/G3; G1 re-enters S0 (I1); CC2 hands over with the recommendation record
    pipeline = turn.pipeline
    if (pipeline is not None and pipeline.overlong) or turn.identity or turn.safety:
        return  # compose answers (ask to shorten, I6, the crisis script)
    turn.products = await product_names(turn)
    if pipeline is not None and pipeline.blocked:
        _choices(turn)  # the turn's content is discarded (injection): structured choices only
        return
    text = pipeline.stored_raw if pipeline is not None else ""
    intents = set(pipeline.analysis.intents) if pipeline and pipeline.analysis else set()
    lexicon = bundle(turn).s3_lexicon
    if (
        kind == "SAVE"
        or Intent.NEED_TIME in intents
        or (text and mentions(lexicon.need_time, text))
    ):
        _need_time(turn)
        return
    if kind == "REJECT_ALL" or Intent.REJECT_ALL in intents:
        _reject_all(turn)
        return
    if Intent.DECLINE in intents:
        _decline(turn)
        return
    try:
        rec = await _revalidated(turn)
        if rec is None:
            return
        if kind == "APPLY":
            await _apply(turn, rec, payload)
        elif kind == "CHEAPER":
            await _cheaper(turn, rec, str(payload.get("uin", "")))
        elif kind == "DISCLOSURE_ACK":
            await _ack(turn, rec, payload)
        elif kind == "REVISE":
            rec.cta = "revise"
            _reply(turn, [("revise_ask", scripts(turn).revise_ask)], [])
        elif kind is not None or pipeline is None:
            _choices(turn)  # CONTINUE, RETRY or anything else structured
        else:
            await _free_text(turn, rec, text, intents)
    except (DomainError, Stale) as exc:
        _down(turn, exc)


async def _revalidated(turn: Any) -> RecommendationPayload | None:
    """TDD §3.8 "returns days later" and the in-S3 events: the recommendation still holds, or this
    turn presents it again (None). No recommendation (a blocked release, or a resume dropped it):
    present; none possible -> S3.0 escalates."""
    current = session(turn)
    rec = current.recommendation
    texts = scripts(turn)
    if rec is None:
        if not await present(turn, []):
            turn.signals["no_options"] = True  # S3.0: the HE handler speaks
            _reply(turn, [("no_option", texts.no_option)], [])
        return None
    stale = await pause._stale(turn, rec)
    if stale is None:
        return rec
    logger.info("recommendation out of date: %s", stale)
    if stale == "QUOTE_EXPIRED" and rec.ranking is not None:
        options = [await _requoted(turn, o) for o in rec.options]
        ranking = rec.ranking.model_copy(update={"options": options})
        await present(turn, [("requote_note", texts.requote_note)], ranking)
    elif not await present(turn, [("rerank_note", texts.rerank_note)]):
        turn.signals["no_options"] = True
        _reply(turn, [("no_option", texts.no_option)], [])
    return None


async def _requoted(turn: Any, option: RecommendedOption) -> RecommendedOption:
    """An expired option quote, quoted again with its own values (TDD §3.8: re-quote)."""
    quote = option.quote
    if quote is None or quote_valid(quote, _today(turn)):
        return option
    fresh, _ = await _quote(
        turn,
        uin=quote.uin,
        sum_assured_inr=quote.sum_assured_inr,
        term_years=quote.term_years,
        ppt=quote.ppt,
        rider_uins=sorted(quote.rider_premiums),
        frequency=quote.frequency,
        extra={"requote_of": quote.quote_id},
    )
    return option.model_copy(update={"quote": fresh})


async def _quote(
    turn: Any,
    *,
    uin: str,
    sum_assured_inr: str,
    term_years: int,
    ppt: Any,
    rider_uins: list[str],
    frequency: Any,
    extra: dict[str, Any],
) -> tuple[PremiumQuote, QuoteRequest]:
    current = session(turn)
    eligibility = cast(EligibilityPayload, current.eligibility)
    request = QuoteRequest(
        pins=Pins(rules=current.pins.rules),
        uin=uin,
        sum_assured_inr=sum_assured_inr,
        term_years=term_years,
        ppt=ppt,
        rider_uins=rider_uins,
        age_years=_life_assured_age(current, _slots(turn)),
        gender=eligibility.gender,
        tobacco_12m=eligibility.tobacco_12m,
        frequency=frequency,
    )
    quote: PremiumQuote = await turn.domain.create_quote(request)
    _record(
        turn,
        "quote",
        str(quote.decision_id),
        quote.inputs_sha256,
        quote.reason_codes,
        {
            "request": request.model_dump(mode="json", exclude_unset=True),
            "result": quote.model_dump(mode="json"),
            **extra,
        },
    )
    logger.info("quote %s issued in S3 (%s)", quote.quote_id, ", ".join(sorted(extra)))
    return quote, request


# --- the rows -------------------------------------------------------------------------------------
def _need_time(turn: Any) -> None:
    """S3.2: Pause, with the options shown kept in the session (the pause handler leads with
    `paused`); the re-engagement line only within the granted purposes (P3); no outbound."""
    current = session(turn)
    texts = scripts(turn)
    turn.signals["need_time"] = True
    rec = current.recommendation
    parts: list[tuple[str, str]] = []
    if rec is not None:
        rec.cta = "save"
        names = handlers_names(turn)
        lines = [
            texts.s3_summary_line.format(
                name=names.get(o.uin, o.uin),
                uin=o.uin,
                cover=format_inr(o.sum_assured_inr),
                term=o.term_years,
                premium=(
                    f"{format_inr(o.quote.annual_premium_inr)} {_period(turn, o.quote)}"
                    if o.quote is not None
                    else texts.s3.premium_not_shown
                ),
            )
            for o in rec.options
        ]
        parts.append(("s3_summary", texts.s3_summary.format(options="\n".join(lines))))
    if current.consent is not None and "P3" in granted(current.consent.purposes):
        parts.append(("reengage", texts.reengage))
    _reply(turn, parts, [])
    logger.info("S3 paused by the customer")


def handlers_names(turn: Any) -> dict[str, str]:
    return dict(turn.products)


def _reject_all(turn: Any) -> None:
    """S3.3 (loops left: S2 with the re-discovery framing, which s2's enter leads with) or S3.3b
    (the HE handler speaks)."""
    current = session(turn)
    turn.signals["all_rejected"] = True
    loops = current.counters.get("rediscovery_loops", 0)
    if loops < turn.settings.rediscovery_loop_limit:
        _reply(turn, [("rediscovery", scripts(turn).rediscovery)], [])
    else:
        _reply(turn, [], [])
    logger.info("all options rejected (loops taken: %d)", loops)


def _decline(turn: Any) -> None:
    """S3.1 after re-discovery (a graceful exit); before it, no row: revisit, save or an advisor."""
    current = session(turn)
    texts = scripts(turn)
    if current.counters.get("rediscovery_loops", 0) >= 1:
        parts = [("declined_exit", texts.declined_exit)]
        if current.consent is not None and "P3" in granted(current.consent.purposes):
            parts.append(("reengage", texts.reengage))
        _reply(turn, parts, [])
        return
    cta = bundle(turn).templates[current.locale].recommendation.cta
    _reply(
        turn,
        [("decline_first", texts.decline_first)],
        [
            quick_reply(texts.s3.revisit, "REJECT_ALL", {}),
            quick_reply(cta.save, "SAVE", {}),
            _advisor(turn),
        ],
    )


def _choices(turn: Any) -> None:
    _reply(turn, [("s3_choices", scripts(turn).s3_choices)], _cta(turn))


# --- the customer's choice ------------------------------------------------------------------------
def _option(rec: RecommendationPayload, uin: Any) -> RecommendedOption | None:
    return next((o for o in rec.options if o.uin == uin), None)


def _choose(turn: Any, rec: RecommendationPayload) -> None:
    labels = scripts(turn).s3
    names = handlers_names(turn)
    quick = []
    for o in rec.options:
        name = names.get(o.uin, o.uin)
        quick.append(quick_reply(labels.apply_plan.format(name=name), "APPLY", {"uin": o.uin}))
        if o.quote is not None:
            quick.append(
                quick_reply(labels.cheaper_plan.format(name=name), "CHEAPER", {"uin": o.uin})
            )
    session(turn).last_prompt_id = CHOOSE
    _reply(turn, [("choose_option", scripts(turn).choose_option)], quick)


def _differs(key: str, wanted: Any, quote: PremiumQuote) -> bool:
    if key == "sum_assured_inr":
        return Decimal(str(wanted)) != Decimal(quote.sum_assured_inr)
    if key == "rider_uins":
        return sorted(wanted) != sorted(quote.rider_premiums)
    return bool(wanted != getattr(quote, key))


async def _apply(turn: Any, rec: RecommendationPayload, payload: dict[str, Any]) -> None:
    """A plan chosen, as offered or with the customer's own cover, term, PPT or riders (then
    priced for exactly that: POST /v1/quotes; the gap is the customer's choice)."""
    texts = scripts(turn)
    uin = payload.get("uin")
    if uin is None and len(rec.options) == 1:
        uin = rec.options[0].uin
    option = _option(rec, uin)
    if option is None:
        _choose(turn, rec)
        return
    rec.cta = "apply"
    quote = option.quote
    if quote is None:  # premium withheld or unrated: no quote to apply on (V7)
        _reply(turn, [("apply_needs_premium", texts.apply_needs_premium)], [_advisor(turn)])
        return
    choice = {k: payload[k] for k in CHOICE_KEYS if payload.get(k) is not None}
    custom = any(_differs(k, v, quote) for k, v in choice.items())
    suitability = cast(SuitabilityResult, session(turn).suitability)
    gap = option.protection_gap_inr
    if custom or not quote_valid(quote, _today(turn)):
        cover = str(choice.get("sum_assured_inr", quote.sum_assured_inr))
        gap = _gap(suitability.recommended_cover_inr, cover)
        try:
            quote, _ = await _quote(
                turn,
                uin=option.uin,
                sum_assured_inr=cover,
                term_years=int(choice.get("term_years", quote.term_years)),
                ppt=choice.get("ppt", quote.ppt),
                rider_uins=sorted(choice.get("rider_uins", quote.rider_premiums)),
                frequency=quote.frequency,
                extra={"customer_choice": custom, "protection_gap_inr": gap},
            )
        except DomainError as exc:
            problem = exc.problem
            part = (
                bounds_part(texts, handlers_names(turn).get(option.uin, option.uin), problem)
                if exc.code == "QUOTE_OUT_OF_BOUNDS" and problem is not None
                else None
            )
            if part is None:
                raise
            _reply(turn, [part], [])
            return
    rec.selection = Selection(
        uin=option.uin, quote=quote, protection_gap_inr=gap, customer_choice=custom
    )
    logger.info("plan chosen: %s (customer's own choice: %s)", option.uin, custom)
    if acks_valid(rec, option.uin):
        await _hand_off(turn, rec, rec.selection)
        return
    await _ask_ack(turn, rec, rec.selection)


def _gap(recommended: str, cover: str) -> str:
    gap = max(Decimal(recommended) - Decimal(cover), Decimal(0))
    return format(gap.normalize(), "f") if gap else "0"


async def _selection_parts(turn: Any, sel: Selection) -> list[tuple[str, str]]:
    """The chosen plan as the quote priced it, from the quote and the catalog (rider names)."""
    texts = scripts(turn)
    labels = texts.labels
    quote = sel.quote
    product = await turn.domain.get_product(sel.uin, as_of=handlers.now())
    riders = {r.uin: r.name for r in product.riders}
    card = texts.selection_card.format(
        name=product.name,
        uin=sel.uin,
        cover=format_inr(quote.sum_assured_inr),
        term=quote.term_years,
        ppt=labels.ppt[quote.ppt],
        riders=", ".join(riders.get(r, r) for r in sorted(quote.rider_premiums)) or labels.none,
        premium=format_inr(quote.annual_premium_inr),
        period=_period(turn, quote),
        quote_id=quote.quote_id,
        valid_until=format_date(quote.valid_until),
        gap=format_inr(sel.protection_gap_inr),
    )
    parts = [("selection_card", card)]
    suitability = session(turn).suitability
    if sel.customer_choice and Decimal(sel.protection_gap_inr) > 0 and suitability is not None:
        parts.append(
            (
                "gap_choice",
                texts.gap_choice.format(cover=format_inr(suitability.recommended_cover_inr)),
            )
        )
    return parts


async def _ask_ack(turn: Any, rec: RecommendationPayload, sel: Selection) -> None:
    """V7: the acknowledgment, bound to the set and the documents this render showed."""
    texts = scripts(turn)
    shown = rec.shown[sel.uin]
    name = handlers_names(turn).get(sel.uin, sel.uin)
    session(turn).last_prompt_id = f"s3.ack:{sel.uin}"
    _reply(
        turn,
        [
            *await _selection_parts(turn, sel),
            ("ack_ask", texts.ack_ask.format(name=name, uin=sel.uin)),
        ],
        [
            quick_reply(
                texts.s3.acknowledge,
                "DISCLOSURE_ACK",
                {
                    "uin": sel.uin,
                    "registry_version": shown.registry_version,
                    "disclosure_set_sha256": shown.set_sha256,
                    "document_sha256": dict(shown.documents),
                },
            ),
            _advisor(turn),
        ],
    )


async def _ack(turn: Any, rec: RecommendationPayload, payload: dict[str, Any]) -> None:
    """The acknowledgment, checked against the registry now and what this render showed. A
    mismatch is refused and the options presented again; a match is stored (conv.disclosure_ack,
    DISCLOSURE_ACK) and, with the plan chosen and its quote valid, the hand-off follows."""
    current, row = session(turn), cast(SessionRow, turn.row)
    texts = scripts(turn)
    uin = payload.get("uin")
    shown = rec.shown.get(str(uin))
    if rec.rec_id is None or shown is None:
        _choices(turn)
        return
    registry = await turn.domain.get_disclosure_set(
        str(uin), row.channel, current.locale, as_of=handlers.now()
    )
    problems = check_ack(payload, shown, registry)
    if problems:
        logger.info("acknowledgment for %s refused: %s", uin, ", ".join(problems))
        if not await present(turn, [("ack_mismatch", texts.ack_mismatch)]):
            turn.signals["no_options"] = True
            _reply(turn, [("no_option", texts.no_option)], [])
        return
    ack = DisclosureAck(
        uin=str(uin),
        registry_version=shown.registry_version,
        disclosure_set_sha256=shown.set_sha256,
        document_sha256=dict(shown.documents),
        acked_at=handlers.now(),
    )
    store.insert_disclosure_ack(turn.conn, rec_id=rec.rec_id, ack=ack)
    append(
        turn,
        EventType.DISCLOSURE_ACK,
        DisclosureAckHeader(
            uin=ack.uin,
            registry_version=ack.registry_version,
            set_sha256=ack.disclosure_set_sha256,
            document_sha256s=sorted(ack.document_sha256.values()),
        ),
        {"ack": ack.model_dump(mode="json"), "rec_id": str(rec.rec_id)},
    )
    rec.acks = [*rec.acks, ack]
    logger.info("disclosures acknowledged for %s", ack.uin)
    sel = rec.selection
    name = handlers_names(turn).get(ack.uin, ack.uin)
    if sel is None or sel.uin != ack.uin:
        _reply(
            turn,
            [("ack_recorded", texts.ack_recorded.format(name=name, uin=ack.uin))],
            [quick_reply(texts.s3.apply_plan.format(name=name), "APPLY", {"uin": ack.uin})],
        )
        return
    if not quote_valid(sel.quote, _today(turn)):  # a new price is shown before anything is sent
        quote = sel.quote
        fresh, _ = await _quote(
            turn,
            uin=quote.uin,
            sum_assured_inr=quote.sum_assured_inr,
            term_years=quote.term_years,
            ppt=quote.ppt,
            rider_uins=sorted(quote.rider_premiums),
            frequency=quote.frequency,
            extra={"requote_of": quote.quote_id},
        )
        rec.selection = sel = sel.model_copy(update={"quote": fresh})
        _reply(
            turn,
            [("requote_note", texts.requote_note), *await _selection_parts(turn, sel)],
            [quick_reply(texts.s3.apply_plan.format(name=name), "APPLY", _apply_payload(sel))],
        )
        return
    await _hand_off(turn, rec, sel)


def _apply_payload(sel: Selection) -> dict[str, Any]:
    quote = sel.quote
    return {
        "uin": sel.uin,
        "sum_assured_inr": quote.sum_assured_inr,
        "term_years": quote.term_years,
        "ppt": quote.ppt,
        "rider_uins": sorted(quote.rider_premiums),
    }


# --- the hand-off adapter (TDD §7.1) --------------------------------------------------------------
async def _hand_off(turn: Any, rec: RecommendationPayload, sel: Selection) -> None:
    """V7 holds: the signed intake to the application journey. Taken: conv.handoff (queue
    application) and HANDOFF, then S3.4 hands off. The journey down: S3 stays, the intake is kept
    for a retry, and an advisor completes it (with P2; otherwise the advisor offer asks for P2)."""
    current, row = session(turn), cast(SessionRow, turn.row)
    settings = turn.settings
    texts = scripts(turn)
    registry = await turn.domain.get_disclosure_set(
        sel.uin, row.channel, current.locale, as_of=handlers.now()
    )
    shown = rec.shown[sel.uin]
    if (registry.registry_version, registry.set_sha256) != (
        shown.registry_version,
        shown.set_sha256,
    ):
        logger.info("registry moved since the acknowledgment: presented again")
        if not await present(turn, [("ack_mismatch", texts.ack_mismatch)]):
            turn.signals["no_options"] = True
        return
    payload = intake.build(
        session_id=str(turn.session_id),
        subject_ref=current.subject_ref,
        quote=sel.quote,
        slots=_slots(turn),
        suitability_inputs_sha256=rec.suitability_inputs_sha256,
        acks=[a for a in rec.acks if a.uin == sel.uin],
        anchor=audit_chain.tail(turn.conn, turn.session_id),
    )
    signed = intake.sign(
        payload, intake.signing_key(settings.intake_signing_key_b64.get_secret_value())
    )
    name = handlers_names(turn).get(sel.uin, sel.uin)
    try:
        accepted = await intake.post(settings.journey_url, signed)
    except intake.IntakeUnavailable as exc:
        _journey_down(turn, signed, exc.reason)
        return
    _store_intake(turn, "APPLICATION_INTAKE", {"intake": signed, "intake_ref": accepted.intake_ref})
    turn.signals["selected"] = {"uin": sel.uin, "quote_valid": True}
    turn.signals["acks_valid_for_selected"] = True
    _reply(
        turn,
        [
            (
                "handoff_application",
                texts.handoff_application.format(
                    name=name, uin=sel.uin, reference=accepted.intake_ref
                ),
            )
        ],
        [],
    )


def _store_intake(turn: Any, reason: str, payload: dict[str, Any]) -> None:
    row = cast(SessionRow, turn.row)
    handoff_id = store.insert_handoff(
        turn.conn,
        turn.keys,
        row.key_ref,
        session_id=turn.session_id,
        reason_code=reason,
        queue="application",
        payload=payload,
    )
    append(
        turn,
        EventType.HANDOFF,
        HandoffHeader(handoff_id=handoff_id, reason_code=reason, queue="application"),
        payload,
    )


def _journey_down(turn: Any, signed: dict[str, Any], reason: str) -> None:
    current = session(turn)
    texts = scripts(turn)
    logger.warning("application journey unavailable: %s; intake kept for a retry", reason)
    _store_intake(turn, "INTAKE_PENDING", {"intake": signed, "delivery": reason})
    consent = current.consent
    if consent is not None and human_escalation.p2_granted(consent):
        human_escalation.hand_off(turn, "HE_JOURNEY_DOWN")
        _reply(turn, [("journey_down", texts.journey_down), *turn.parts], [])
    else:
        _reply(turn, [("journey_down", texts.journey_down)], [_advisor(turn)])


# --- make it cheaper ------------------------------------------------------------------------------
async def _cheaper(turn: Any, rec: RecommendationPayload, uin: str) -> None:
    """TDD §3.8 "Make it cheaper": the engine's alternatives for the option's own quote, each with
    its protection gap against the recommended cover, and an APPLY quick reply for each."""
    texts = scripts(turn)
    option = _option(rec, uin)
    if option is None:
        _choose(turn, rec)
        return
    quote = option.quote
    if quote is None:
        _reply(turn, [("cheaper_unavailable", texts.cheaper_unavailable)], [_advisor(turn)])
        return
    current = session(turn)
    eligibility = cast(EligibilityPayload, current.eligibility)
    suitability = cast(SuitabilityResult, current.suitability)
    request = QuoteAlternativesRequest(
        pins=Pins(rules=current.pins.rules),
        uin=uin,
        sum_assured_inr=quote.sum_assured_inr,
        term_years=quote.term_years,
        ppt=quote.ppt,
        rider_uins=sorted(quote.rider_premiums),
        age_years=_life_assured_age(current, _slots(turn)),
        gender=eligibility.gender,
        tobacco_12m=eligibility.tobacco_12m,
        frequency=quote.frequency,
        recommended_cover_inr=suitability.recommended_cover_inr,
    )
    alternatives = await turn.domain.create_quote_alternatives(request)
    sent = request.model_dump(mode="json", exclude_unset=True)
    _record(
        turn,
        "alternatives",
        str(alternatives[0].quote.decision_id) if alternatives else "none",
        sha256_hex(sent),  # the contract's rule: SHA-256(JCS(the request as sent))
        [a.change for a in alternatives],
        {"request": sent, "result": [a.model_dump(mode="json") for a in alternatives]},
    )
    logger.info("alternatives for %s: %d", uin, len(alternatives))
    labels, s3_labels = texts.labels, texts.s3
    product = await turn.domain.get_product(uin, as_of=handlers.now())
    riders = {r.uin: r.name for r in product.riders}
    name = handlers_names(turn).get(uin, product.name)
    lines = [texts.alternatives.format(name=name, uin=uin)]
    quick = []
    for n, alt in enumerate(alternatives, start=1):
        q = alt.quote
        lines.append(
            texts.alternative_line.format(
                n=n,
                change=s3_labels.changes[alt.change],
                cover=format_inr(q.sum_assured_inr),
                ppt=labels.ppt[q.ppt],
                riders=", ".join(riders.get(r, r) for r in sorted(q.rider_premiums)) or labels.none,
                premium=format_inr(q.annual_premium_inr),
                period=_period(turn, q),
                gap=format_inr(alt.protection_gap_inr),
            )
        )
        quick.append(
            quick_reply(
                s3_labels.alternative.format(n=n),
                "APPLY",
                {
                    "uin": uin,
                    "sum_assured_inr": q.sum_assured_inr,
                    "term_years": q.term_years,
                    "ppt": q.ppt,
                    "rider_uins": sorted(q.rider_premiums),
                },
            )
        )
    current.last_prompt_id = f"s3.alternatives:{uin}"
    _reply(turn, [("alternatives", "\n".join(lines))], [*quick, _advisor(turn)])


# --- free text ------------------------------------------------------------------------------------
async def _free_text(
    turn: Any, rec: RecommendationPayload, text: str, intents: set[Intent]
) -> None:
    texts = scripts(turn)
    lexicon = bundle(turn).s3_lexicon
    if mentions(lexicon.which_one, text):
        _which_one(turn, rec)
        return
    named = products_named(normalise(text).text, turn.products) - {o.uin for o in rec.options}
    if named:
        await _not_recommended(turn, sorted(named)[0])
        return
    if Intent.OBJECTION_PRICE in intents or mentions(lexicon.cheaper, text):
        await _cheaper(turn, rec, rec.options[0].uin)
        return
    dispute = mentions(lexicon.exclusion_dispute, text)
    analysis = turn.pipeline.analysis if turn.pipeline is not None else None
    guarantee = Intent.OBJECTION_GUARANTEE in intents or mentions(
        bundle(turn).needs_lexicon.guarantee, text
    )
    question = "?" in text or bool(intents & QUESTIONS) or bool(analysis and analysis.side_query)
    if dispute or guarantee or question:
        query = (analysis.side_query if analysis else None) or text
        await answer(turn, rec, query, intents, dispute=dispute, guarantee=guarantee)
        return
    if Intent.OFF_TOPIC in intents:
        _reply(turn, [("redirect", texts.redirect)], _cta(turn))
        return
    _choices(turn)


def _which_one(turn: Any, rec: RecommendationPayload) -> None:
    """The top option and the reasons it was placed first; the decision stays the customer's."""
    texts = scripts(turn)
    reasons = texts.s3.reasons
    top = rec.options[0]
    lines = list(dict.fromkeys(reasons.get(c, reasons["default"]) for c in top.reason_codes))
    _reply(
        turn,
        [
            (
                "which_one",
                texts.which_one.format(
                    name=handlers_names(turn).get(top.uin, top.uin),
                    uin=top.uin,
                    reasons="; ".join(lines) or reasons["default"],
                ),
            )
        ],
        _cta(turn),
    )


async def _not_recommended(turn: Any, uin: str) -> None:
    """A plan the customer named that is not an option: why, from the engines' current decisions
    for the confirmed inputs (eligible UINs; suitability's fit types). An advisor if they insist."""
    current = session(turn)
    texts = scripts(turn)
    engine = current.eligibility.engine if current.eligibility else None
    suitability = current.suitability
    why: Literal["not_eligible", "not_fit", "default"] = "default"
    if engine is not None and uin not in engine.eligible_uins:
        why = "not_eligible"
    else:
        try:
            product = await turn.domain.get_product(uin, as_of=handlers.now())
        except DomainError as exc:
            if exc.code != "NOT_FOUND":
                raise
        else:
            if suitability is not None and product.category not in suitability.fit_types:
                why = "not_fit"
    _reply(
        turn,
        [
            (
                "not_recommended",
                texts.not_recommended.format(
                    name=handlers_names(turn).get(uin, uin),
                    uin=uin,
                    reason=texts.s3.not_recommended[why],
                ),
            )
        ],
        _cta(turn),
    )
    logger.info("a plan that is not an option was named: %s", why)


async def answer(
    turn: Any,
    rec: RecommendationPayload,
    query: str,
    intents: set[Intent],
    *,
    dispute: bool,
    guarantee: bool,
) -> None:
    """A question about the options, answered only from the evidence, cited and verified (the
    side-query L1 on gen-recommend; the output rails): tax with the regime condition and
    DISC-GLOBAL-TAX-05 verbatim, never a personal computation; an exclusion dispute with the
    wording and the grievance route; a guarantee only as the wording defines one. Insufficient
    evidence: "I can't confirm that" and an advisor. Step 22's side-query subgraph lifts it out."""
    current = session(turn)
    texts = scripts(turn)
    uins = [o.uin for o in rec.options]
    retrieval = await _retrieve(turn, query, uins, intents=sorted(intents))
    tax = (retrieval is not None and retrieval.audit.route_rule == "RT-TAX") or bool(
        _TAX.search(query)
    )
    lead = [("guarantee_note", texts.guarantee_note)] if guarantee else []
    after = [("side_query_caveat", texts.side_query_caveat["S3"])]
    if tax:
        disclosure = await turn.domain.get_disclosure(TAX_05, current.locale)
        turn.shown.append(disclosure.body)
        after += [("tax_condition", texts.tax_condition), (f"registry:{TAX_05}", disclosure.body)]
    if dispute:
        after.append(("exclusion_note", texts.exclusion_note))
    if retrieval is None or retrieval.abstained:
        _reply(turn, [*lead, ("abstain", texts.abstain), *after], _cta(turn))
        logger.info("S3 question: no sufficient evidence, abstained")
        return
    facts = _engine_facts(turn, rec.options, turn.products)
    draft, handles = await _generate(turn, "side-query", retrieval, facts)
    sources = bundle(turn).templates[current.locale].recommendation

    def render_(narrative: str | None) -> Rendered:
        if narrative is None:
            body = [("abstain", texts.abstain)]
            cited_parts: list[tuple[str, str]] = []
            citations: dict[str, str] = {}
            source_items: list[Any] = []
        else:
            cited = render(narrative, handles)
            body = [("generated:answer", cited.text)]
            cited_parts = (
                [("sources", source_list(cited.sources, sources))] if cited.sources else []
            )
            citations = handles.evidence_map(cited.handles)
            source_items = cited.sources
        parts = [
            (i if ":" in i else f"template:{i}", t) for i, t in [*lead, *body, *after, *cited_parts]
        ]
        joined = "\n\n".join(t for _, t in parts)
        return Rendered(joined, sha256_hex_text(joined), parts, citations, source_items, {}, {})

    async def regenerate(errors: list[str]) -> str | None:
        return (await _generate(turn, "side-query", retrieval, facts, errors))[0]

    turn.draft, turn.render, turn.regenerate, turn.handles = draft, render_, regenerate, handles
    _reply(turn, [], _cta(turn))
    logger.info("S3 question answered from %d evidence chunks", len(retrieval.evidence))
