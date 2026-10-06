"""State-3 (Step 21): the recommendation (the rank request, the parts, the four equal choices, the
recorded render), entry outcomes (no option, the ranker down, a registry integrity failure, the
generation down or failing twice), the customer's choice (as offered, a re-quote of their own
riders or cover, the bounds), the hash-bound acknowledgment and the signed hand-off (and the journey
down), "make it cheaper", revalidation (an expired quote, a kill switch), the free-text edges
(which one, a plan not offered, tax, an exclusion dispute, a guarantee, no evidence) and the rows
(need time, all rejected, the third rejection, a decline). The domain tier is runtime_support's
MockTransport stand-in; the store and the clock are patched; audit appends are recorded."""

import json
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest
from compose_support import chunk, retrieved
from runtime_support import (
    CITED,
    ROP,
    TERM,
    Models,
    domain,
    domain_handler,
    problem,
    seed_set,
    suitability_json,
)
from test_runtime_nodes import BUNDLE, Recorder, StoreCalls, row, rt, run, session, turn
from test_s0 import transition, valid_record
from test_s1 import TDD
from test_s2 import NEEDS, eligible

from surakshasetu.audit import chain as audit_chain
from surakshasetu.domain.models import NeedsPayload, SuitabilityResult
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import handlers, nodes
from surakshasetu.graph.nodes import Turn
from surakshasetu.graph.state import GraphState, SessionState
from surakshasetu.graph.states import s2, s3
from surakshasetu.handoff import intake
from surakshasetu.store import conv as store

SCRIPTS = BUNDLE.templates["en-IN"].scripts
REC = BUNDLE.templates["en-IN"].recommendation
NOW = datetime(2026, 10, 5, 6, 0, tzinfo=UTC)  # the stand-in quotes are valid until 4 Nov 2026
REC_ID = UUID("0199a1b2-0000-7000-8000-0000000000ec")
RANK, QUOTES, ALTERNATIVES = "/v1/ranking/rank", "/v1/quotes", "/v1/quotes/alternatives"
PARTS = [
    "needs_recap",
    f"option_card:{TERM}",
    f"option_card:{ROP}",
    "comparison",
    "why_it_fits",
    f"disclosures:{TERM}",
    f"disclosures:{ROP}",
    "cta",
]


class Tier:
    """runtime_support's domain stand-in, recording every call. `down` lists unreachable path
    prefixes; `answers` replaces a path's response; `no_option` empties the ranking."""

    def __init__(
        self,
        *,
        down: tuple[str, ...] = (),
        answers: dict[str, httpx.Response] | None = None,
        no_option: bool = False,
    ) -> None:
        self.down, self.answers, self.no_option = down, answers or {}, no_option
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        if any(request.url.path.startswith(d) for d in self.down):
            raise httpx.ConnectError("down")
        for prefix, answer in self.answers.items():
            if request.url.path.startswith(prefix):
                return answer
        if request.url.path == RANK and self.no_option:
            body["eligible_uins"] = []
        if request.url.path == RANK:
            request = httpx.Request("POST", request.url, json=body)
        return domain_handler(request)

    def bodies(self, path: str) -> list[Any]:
        return [b for p, b in self.calls if p == path]


class Journey:
    """The application journey (handoff.intake.post): takes the intake unless down."""

    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []
        self.down = False

    async def post(self, url: str, signed: dict[str, Any], **_: Any) -> intake.Accepted:
        self.posted.append(signed)
        if self.down:
            raise intake.IntakeUnavailable("UNAVAILABLE")
        return intake.Accepted("INT-000001", replayed=False)


class Stored:
    """The store's S3 writes, as calls."""

    def __init__(self) -> None:
        self.recommendations: list[Any] = []
        self.acks: list[Any] = []
        self.handoffs: list[dict[str, Any]] = []


@pytest.fixture(autouse=True)
def audited(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    return Recorder(monkeypatch)


@pytest.fixture(autouse=True)
def stored(monkeypatch: pytest.MonkeyPatch) -> Stored:
    found = {**TDD, **NEEDS}
    writes = Stored()
    monkeypatch.setattr(
        store, "latest_slots", lambda *a: {k: ("confirmed", v) for k, v in found.items()}
    )
    monkeypatch.setattr(store, "current_slots", lambda *a: dict(found))
    monkeypatch.setattr(
        store,
        "insert_recommendation",
        lambda conn, *, session_id, payload: writes.recommendations.append(payload) or REC_ID,
    )
    monkeypatch.setattr(
        store, "insert_disclosure_ack", lambda conn, *, rec_id, ack: writes.acks.append(ack)
    )

    def handoff(conn: Any, keys: Any, key_ref: str, **row_: Any) -> UUID:
        writes.handoffs.append(row_)
        return UUID(int=len(writes.handoffs))

    monkeypatch.setattr(store, "insert_handoff", handoff)
    monkeypatch.setattr(audit_chain, "tail", lambda conn, sid: (7, b"\x07" * 32))
    monkeypatch.setattr(audit_chain, "events", lambda conn, sid: [])
    monkeypatch.setattr(handlers, "_PRODUCT_NAMES", {})
    monkeypatch.setattr(handlers, "now", lambda: NOW)
    return writes


@pytest.fixture(autouse=True)
def journey(monkeypatch: pytest.MonkeyPatch) -> Journey:
    j = Journey()
    monkeypatch.setattr(intake, "post", j.post)
    return j


def bound() -> dict[str, Any]:
    """The needs and the FIT record S2 bound (I2)."""
    needs = NeedsPayload.model_validate(
        NEEDS | {"financial_distress": False, "comprehension_difficulty_count": 0}
    )
    digest = s2.needs_sha256(needs)
    result = SuitabilityResult.model_validate(
        suitability_json({"needs": needs.model_dump(mode="json", exclude_unset=True)})
    )
    assert result.inputs_sha256 == digest
    return {"needs": needs.model_copy(update={"slots_sha256": digest}), "suitability": result}


def s3_turn(
    text: str | None = None,
    *,
    action: dict[str, Any] | None = None,
    models: Models | None = None,
    tier: Tier | None = None,
    held: SessionState | None = None,
    consent: tuple[str, ...] = ("P1",),
    retrieval: Any = None,
    **update: Any,
) -> Turn:
    t = turn(models, text=text)
    t.action = action if text is None else None
    t.domain = domain(tier or Tier())
    t.retrieval = retrieval
    if held is None:
        base = {
            "fsm_state": FsmState.S3,
            "consent": valid_record(*consent),
            "eligibility": eligible(),
            **bound(),
        }
        t.next = session(**(base | update))
    else:  # the next turn on the session the last one left, through the checkpoint's JSON
        t.next = SessionState.model_validate(held.model_dump(mode="json")).model_copy(update=update)
    t.row = row(fsm_state=t.next.fsm_state.value)
    t.from_state = t.next.fsm_state
    return t


async def entered(t: Turn) -> Turn:
    """S2.2 just moved the session to S3: enter, compose, validate (as the graph runs them)."""
    await s3.enter(GraphState(), runtime=rt(t))
    await nodes.compose(GraphState(), rt(t))
    await nodes.validate(GraphState(), rt(t))
    return t


async def presented(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> SessionState:
    """An S3 entry, committed: the session holds the recorded recommendation."""
    t = await entered(s3_turn(**kw))
    StoreCalls(monkeypatch)
    await nodes.commit(GraphState(), rt(t))
    assert t.next is not None and t.next.recommendation is not None
    return t.next


async def follow(held: SessionState, text: str | None = None, **kw: Any) -> Turn:
    t = s3_turn(text, held=held, **kw)
    await run(t)
    return t


def ids(t: Turn) -> list[str]:
    assert t.released is not None and t.released.rendered is not None
    return [i for i, _ in t.released.rendered.parts]


def kinds(t: Turn) -> list[str]:
    return [r["action"]["type"] for r in t.quick_replies]


def events(audited: Recorder, kind: str) -> list[dict[str, Any]]:
    return [e for e in audited.events if e["event_type"].value == kind]


def ack_action(t: Turn) -> dict[str, Any]:
    found = next(r for r in t.quick_replies if r["action"]["type"] == "DISCLOSURE_ACK")
    return dict(found["action"])


# --- the recommendation ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_entering_ranks_under_the_pins_with_the_s1_facts_and_records_it(
    audited: Recorder,
) -> None:
    tier = Tier()
    t = await entered(s3_turn(tier=tier))

    [request] = tier.bodies(RANK)
    assert request["pins"] == {"rules": "2026.09.1"}
    assert request["eligible_uins"] == [TERM, ROP]
    assert request["suitability"]["inputs_sha256"] == t.next.needs.slots_sha256  # type: ignore[union-attr]
    assert request["excluded_uins"] == [] and request["flags"] == []
    assert (request["age_years"], request["tobacco_12m"]) == (34, False)
    assert (request["channel"], request["language"]) == ("web", "en-IN")
    assert request["as_of"].startswith("2026-10-05T06:00:00")
    [decision] = [e for e in events(audited, "ENGINE_DECISION")]
    assert decision["header"].service == "ranking"
    assert decision["payload"]["request"] == request  # exactly as sent
    assert [p for p, _ in tier.calls].count("/v1/disclosures/sets/999N001V02") == 1


@pytest.mark.asyncio
async def test_the_recommendation_is_composed_cited_and_ends_with_four_equal_choices() -> None:
    t = await entered(s3_turn())

    assert ids(t) == PARTS
    assert t.released is not None and t.released.kind == "narrative"
    why = dict(t.released.rendered.parts)["why_it_fits"]  # type: ignore[union-attr]
    assert why.startswith("This option fits the needs you confirmed [Source: Our ranking of")
    assert kinds(t) == ["APPLY", "HUMAN_REQUEST", "REVISE", "SAVE"]
    assert [r["label"] for r in t.quick_replies] == [
        REC.cta.apply,
        REC.cta.advisor,
        REC.cta.revise,
        REC.cta.save,
    ]
    assert t.released.verdicts["release:RC-DISCLOSURE"] == "pass"
    disclosures = dict(t.released.rendered.parts)[f"disclosures:{TERM}"]  # type: ignore[union-attr]
    assert disclosures.endswith("\n".join(i.body for i in seed_set(TERM).items))


@pytest.mark.asyncio
async def test_the_commit_records_the_recommendation_and_what_was_shown(
    monkeypatch: pytest.MonkeyPatch, stored: Stored
) -> None:
    held = await presented(monkeypatch)

    [recorded] = stored.recommendations
    rec = held.recommendation
    assert rec is not None and rec.rec_id == REC_ID
    assert recorded.suitability_inputs_sha256 == held.needs.slots_sha256  # type: ignore[union-attr]
    assert recorded.rendered_sha256 and recorded.rendered_sha256 == rec.rendered_sha256
    assert recorded.evidence_map == {"R1": "RANK-FIT-TERM"}
    assert rec.shown[TERM].set_sha256 == seed_set(TERM).set_sha256
    assert rec.shown[TERM].registry_version == "2026.09.1"
    assert set(rec.shown[TERM].documents) == {"CIS", "POLICY_WORDING"}
    assert held.last_prompt_id == s3.OPTIONS


@pytest.mark.asyncio
async def test_a_kill_switched_product_is_left_out_of_the_ranking() -> None:
    tier = Tier()
    t = s3_turn(tier=tier)
    t.kill_switches = {("product", ROP)}

    await entered(t)

    assert tier.bodies(RANK)[0]["excluded_uins"] == [ROP]
    assert f"option_card:{ROP}" not in ids(t)


@pytest.mark.asyncio
async def test_a_withheld_premium_shows_cover_and_features_without_one() -> None:
    elig = eligible()
    engine = elig.engine.model_copy(update={"flags": ["PREMIUM_WITHHELD"]})  # type: ignore[union-attr]
    t = await entered(s3_turn(eligibility=elig.model_copy(update={"engine": engine})))

    card = dict(t.released.rendered.parts)[f"option_card:{TERM}"]  # type: ignore[union-attr]
    assert REC.premium_withheld in card and "Cover: ₹3,75,00,000" in card


@pytest.mark.asyncio
async def test_a_partial_profile_labels_every_option() -> None:
    held = bound()
    partial = held["suitability"].model_copy(update={"profile_sufficiency": 0.65})
    t = await entered(s3_turn(suitability=partial, needs=held["needs"]))

    parts = dict(t.released.rendered.parts)  # type: ignore[union-attr]
    assert parts[f"option_card:{TERM}"].endswith(REC.partial_profile)
    assert parts[f"option_card:{ROP}"].endswith(REC.partial_profile)


# --- entry outcomes -------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_option_says_so_and_the_next_turn_escalates_he_no_option() -> None:
    t = await entered(s3_turn(tier=Tier(no_option=True)))

    assert ids(t) == ["template:no_option"]
    assert kinds(t) == ["CONTINUE"]
    nxt = await follow(
        t.next, action={"type": "CONTINUE", "payload": {}}, tier=Tier(no_option=True)
    )  # type: ignore[arg-type]
    assert transition(nxt) == ("S3.0", FsmState.HUMAN_ESCALATION)
    assert nxt.transition is not None and nxt.transition.reason_code == "HE_NO_OPTION"


@pytest.mark.asyncio
async def test_the_ranker_down_on_entry_offers_a_retry_and_down_again_pauses() -> None:
    down = Tier(down=(RANK,))
    t = await entered(s3_turn(tier=down))

    assert ids(t) == ["template:rec_retry"] and kinds(t) == ["RETRY"]
    nxt = await follow(t.next, action={"type": "RETRY", "payload": {}}, tier=Tier(down=(RANK,)))  # type: ignore[arg-type]
    assert transition(nxt) == ("CC3", FsmState.PAUSE)
    assert ids(nxt)[0] == "template:dependency_down"


@pytest.mark.asyncio
async def test_a_registry_integrity_failure_shows_no_option_without_its_disclosures(
    monkeypatch: pytest.MonkeyPatch, stored: Stored
) -> None:
    broken = problem(500, "REGISTRY_INTEGRITY")
    t = await entered(s3_turn(tier=Tier(answers={"/v1/disclosures/sets/": broken})))

    assert ids(t) == ["template:release_blocked", "template:advisor_offer"]
    assert kinds(t) == ["HUMAN_REQUEST"]
    StoreCalls(monkeypatch)
    await nodes.commit(GraphState(), rt(t))
    assert stored.recommendations == [] and t.response["message"]["disclosures"] == []  # type: ignore[index]


@pytest.mark.asyncio
async def test_gen_recommend_down_gives_the_deterministic_card() -> None:
    t = await entered(s3_turn(models=Models(down=("gen-recommend",))))

    assert t.released is not None and t.released.kind == "fallback"
    why = dict(t.released.rendered.parts)["why_it_fits"]  # type: ignore[union-attr]
    assert why == f"{REC.deterministic_card}\n{SCRIPTS.advisor_offer}"


@pytest.mark.asyncio
async def test_two_failing_drafts_give_the_deterministic_card(audited: Recorder) -> None:
    uncited = "It costs 5000 a year [R1]."
    t = await entered(s3_turn(models=Models(recommend=(uncited, uncited))))

    assert t.released is not None and t.released.kind == "fallback"
    number = [
        e["header"].action
        for e in events(audited, "GUARD_VERDICT")
        if e["header"].rule_id == "GR-NUMBER"
    ]
    assert number == ["regenerate", "fallback"]
    assert len(events(audited, "MODEL_CALL")) == 2


# --- the customer's choice ------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_apply_without_a_plan_asks_which(monkeypatch: pytest.MonkeyPatch) -> None:
    held = await presented(monkeypatch)

    t = await follow(held, action={"type": "APPLY", "payload": {}})

    assert ids(t) == ["template:choose_option"]
    assert [(r["action"]["type"], r["action"]["payload"].get("uin")) for r in t.quick_replies] == [
        ("APPLY", TERM),
        ("CHEAPER", TERM),
        ("APPLY", ROP),
        ("CHEAPER", ROP),
    ]


@pytest.mark.asyncio
async def test_apply_as_offered_asks_for_the_acknowledgment_of_what_was_shown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    tier = Tier()

    t = await follow(held, action={"type": "APPLY", "payload": {"uin": TERM}}, tier=tier)

    assert tier.bodies(QUOTES) == []  # the option's own quote
    assert ids(t) == ["template:selection_card", "template:ack_ask"]
    shown = held.recommendation.shown[TERM]  # type: ignore[union-attr]
    assert ack_action(t)["payload"] == {
        "uin": TERM,
        "registry_version": shown.registry_version,
        "disclosure_set_sha256": shown.set_sha256,
        "document_sha256": shown.documents,
    }
    assert transition(t) == ("S3.STAY", FsmState.S3)


@pytest.mark.asyncio
async def test_a_rider_set_of_the_customers_own_is_quoted_exactly(
    monkeypatch: pytest.MonkeyPatch, audited: Recorder
) -> None:
    held = await presented(monkeypatch)
    tier = Tier()

    t = await follow(
        held,
        action={"type": "APPLY", "payload": {"uin": TERM, "rider_uins": ["999A007V01"]}},
        tier=tier,
    )

    [request] = tier.bodies(QUOTES)
    assert request["uin"] == TERM and request["rider_uins"] == ["999A007V01"]
    assert (request["sum_assured_inr"], request["term_years"]) == ("37500000", 26)
    assert (request["ppt"], request["frequency"], request["age_years"]) == ("regular", "annual", 34)
    quote = events(audited, "ENGINE_DECISION")[-1]
    assert (
        quote["payload"]["customer_choice"] is True
        and quote["payload"]["protection_gap_inr"] == "0"
    )
    card = dict(t.released.rendered.parts)["template:selection_card"]  # type: ignore[union-attr]
    assert "Riders: Accidental death benefit" in card
    assert "Indicative premium: ₹71,250 a year" in card  # 60,000 + 11,250
    assert t.next.recommendation.selection.customer_choice  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_less_cover_records_the_protection_gap_as_the_customers_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)

    t = await follow(
        held, action={"type": "APPLY", "payload": {"uin": TERM, "sum_assured_inr": "28000000"}}
    )

    assert ids(t) == ["template:selection_card", "template:gap_choice", "template:ack_ask"]
    assert t.next.recommendation.selection.protection_gap_inr == "9500000"  # type: ignore[union-attr]
    assert "Protection gap against your recommended cover: ₹95,00,000" in t.released.text  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_a_refused_cover_gets_the_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    held = await presented(monkeypatch)

    t = await follow(
        held, action={"type": "APPLY", "payload": {"uin": TERM, "sum_assured_inr": "100"}}
    )

    assert ids(t) == ["template:cover_bounds"]
    assert t.next.recommendation.selection is None  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_the_acknowledgment_hands_off_with_a_signed_intake(
    monkeypatch: pytest.MonkeyPatch, stored: Stored, journey: Journey, audited: Recorder
) -> None:
    held = await presented(monkeypatch)
    chosen = await follow(
        held, action={"type": "APPLY", "payload": {"uin": TERM, "rider_uins": ["999A007V01"]}}
    )

    t = await follow(chosen.next, action=ack_action(chosen))  # type: ignore[arg-type]

    [ack] = stored.acks
    assert ack.disclosure_set_sha256 == seed_set(TERM).set_sha256
    [header] = [e["header"] for e in events(audited, "DISCLOSURE_ACK")]
    assert header.set_sha256 == ack.disclosure_set_sha256
    [signed] = journey.posted
    public = intake.public_key_b64(
        intake.signing_key(t.settings.intake_signing_key_b64.get_secret_value())
    )
    assert intake.verify(signed, public)
    assert signed["selected"]["rider_uins"] == ["999A007V01"]
    assert signed["selected"]["quote_id"] == chosen.next.recommendation.selection.quote.quote_id  # type: ignore[union-attr]
    assert signed["audit_anchor"] == {"session_seq": 7, "hash": "07" * 32}
    assert signed["disclosure_acks"] == [
        {"uin": TERM, "registry_version": "2026.09.1", "set_sha256": seed_set(TERM).set_sha256}
    ]
    [handoff] = stored.handoffs
    assert (handoff["queue"], handoff["reason_code"]) == ("application", "APPLICATION_INTAKE")
    assert handoff["payload"]["intake_ref"] == "INT-000001"
    assert transition(t) == ("S3.4", FsmState.HANDOFF)
    assert ids(t) == ["template:handoff_application"]


@pytest.mark.asyncio
async def test_a_mismatched_acknowledgment_is_refused_and_the_options_shown_again(
    monkeypatch: pytest.MonkeyPatch, stored: Stored, journey: Journey
) -> None:
    held = await presented(monkeypatch)
    chosen = await follow(held, action={"type": "APPLY", "payload": {"uin": TERM}})
    wrong = ack_action(chosen)
    wrong["payload"] = {**wrong["payload"], "disclosure_set_sha256": "00" * 32}

    t = await follow(chosen.next, action=wrong)  # type: ignore[arg-type]

    assert stored.acks == [] and journey.posted == []
    assert ids(t)[:2] == ["template:ack_mismatch", "needs_recap"]
    assert transition(t) == ("S3.STAY", FsmState.S3)


@pytest.mark.asyncio
async def test_an_acknowledgment_first_then_apply_hands_off(
    monkeypatch: pytest.MonkeyPatch, journey: Journey
) -> None:
    held = await presented(monkeypatch)
    shown = held.recommendation.shown[TERM]  # type: ignore[union-attr]
    payload = {
        "uin": TERM,
        "registry_version": shown.registry_version,
        "disclosure_set_sha256": shown.set_sha256,
        "document_sha256": shown.documents,
    }

    acked = await follow(held, action={"type": "DISCLOSURE_ACK", "payload": payload})
    assert ids(acked) == ["template:ack_recorded"] and kinds(acked) == ["APPLY"]
    t = await follow(acked.next, action={"type": "APPLY", "payload": {"uin": TERM}})  # type: ignore[arg-type]

    assert transition(t) == ("S3.4", FsmState.HANDOFF) and len(journey.posted) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("p2", [True, False])
async def test_the_journey_down_keeps_s3_saves_the_intake_and_an_advisor_completes_it(
    monkeypatch: pytest.MonkeyPatch, stored: Stored, journey: Journey, p2: bool
) -> None:
    consent = ("P1", "P2") if p2 else ("P1",)
    held = await presented(monkeypatch, consent=consent)
    chosen = await follow(held, action={"type": "APPLY", "payload": {"uin": TERM}})
    journey.down = True

    t = await follow(chosen.next, action=ack_action(chosen))  # type: ignore[arg-type]

    assert transition(t) == ("S3.STAY", FsmState.S3)
    queued = [(h["queue"], h["reason_code"]) for h in stored.handoffs]
    pending = stored.handoffs[0]["payload"]["intake"]
    assert pending["selected"]["uin"] == TERM and pending["signature"]  # kept for a retry
    if p2:
        assert queued == [("application", "INTAKE_PENDING"), ("advisor", "HE_JOURNEY_DOWN")]
        assert ids(t) == ["template:journey_down", "template:handoff"]
    else:
        assert queued == [("application", "INTAKE_PENDING")]
        assert ids(t) == ["template:journey_down"] and kinds(t) == ["HUMAN_REQUEST"]


@pytest.mark.asyncio
async def test_cheaper_lists_the_engines_alternatives_with_their_gaps(
    monkeypatch: pytest.MonkeyPatch, audited: Recorder
) -> None:
    held = await presented(monkeypatch)
    tier = Tier()

    t = await follow(held, action={"type": "CHEAPER", "payload": {"uin": TERM}}, tier=tier)

    [request] = tier.bodies(ALTERNATIVES)
    assert request["recommended_cover_inr"] == "37500000"
    assert request["rider_uins"] == ["999A007V01", "999A008V01", "999A009V01"]
    text = dict(t.released.rendered.parts)["template:alternatives"]  # type: ignore[union-attr]
    assert "1. Lower cover: cover ₹2,81,25,000" in text and "Protection gap: ₹93,75,000." in text
    assert "3. Fewer riders: cover ₹3,75,00,000" in text
    first = t.quick_replies[0]["action"]
    assert first == {
        "type": "APPLY",
        "payload": {
            "uin": TERM,
            "sum_assured_inr": "28125000",
            "term_years": 26,
            "ppt": "regular",
            "rider_uins": ["999A007V01", "999A008V01", "999A009V01"],
        },
    }
    assert events(audited, "ENGINE_DECISION")[-1]["header"].service == "alternatives"


@pytest.mark.asyncio
async def test_make_it_cheaper_in_words_offers_the_top_options_alternatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    tier = Tier()

    await follow(held, "can you make it cheaper", tier=tier)

    assert tier.bodies(ALTERNATIVES)[0]["uin"] == TERM


# --- revalidation ---------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_expired_quote_is_quoted_again_with_its_own_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    monkeypatch.setattr(handlers, "now", lambda: datetime(2026, 11, 6, 6, 0, tzinfo=UTC))
    tier = Tier()

    t = await follow(held, action={"type": "APPLY", "payload": {"uin": TERM}}, tier=tier)

    assert tier.bodies(RANK) == []
    requotes = tier.bodies(QUOTES)
    term = next(r for r in requotes if r["uin"] == TERM)
    assert term["rider_uins"] == ["999A007V01", "999A008V01", "999A009V01"]
    assert (term["sum_assured_inr"], term["term_years"], term["ppt"], term["frequency"]) == (
        "37500000",
        26,
        "regular",
        "annual",
    )
    assert ids(t)[:2] == ["template:requote_note", "needs_recap"]


@pytest.mark.asyncio
async def test_a_kill_switch_mid_s3_ranks_again_without_the_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    tier = Tier()
    t = s3_turn(action={"type": "CONTINUE", "payload": {}}, held=held, tier=tier)
    t.kill_switches = {("product", ROP)}

    await run(t)

    assert tier.bodies(RANK)[0]["excluded_uins"] == [ROP]
    assert ids(t)[:3] == ["template:rerank_note", "needs_recap", f"option_card:{TERM}"]


# --- free text ------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_which_one_restates_the_top_option_and_its_reasons(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)

    t = await follow(held, "Which one should I buy?")

    text = dict(t.released.rendered.parts)["template:which_one"]  # type: ignore[union-attr]
    assert "Suraksha Term Shield (999N001V02)" in text and "the choice is yours" in text.lower()
    assert SCRIPTS.s3.reasons["RANK-FIT-TERM"] in text
    assert kinds(t) == ["APPLY", "HUMAN_REQUEST", "REVISE", "SAVE"]


@pytest.mark.asyncio
async def test_a_plan_that_is_not_an_option_is_explained_with_an_advisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)

    t = await follow(held, "what about the Suraksha Saver Guarantee?")

    text = dict(t.released.rendered.parts)["template:not_recommended"]  # type: ignore[union-attr]
    assert SCRIPTS.s3.not_recommended["not_eligible"] in text
    assert "HUMAN_REQUEST" in kinds(t)


class Evidence:
    def __init__(self, result: Any) -> None:
        self.result, self.asked = result, []

    async def retrieve(self, query: str, ctx: Any) -> Any:
        self.asked.append((query, ctx))
        return self.result


def tax_evidence() -> Any:
    found = retrieved(
        [
            chunk(
                "E1",
                domain="tax",
                text="DUMMY: Premiums qualify for a deduction under the old regime only.",
            )
        ],
        {"tax": 1},
    )
    return found.model_copy(
        update={"audit": found.audit.model_copy(update={"route_rule": "RT-TAX"})}
    )


@pytest.mark.asyncio
async def test_a_tax_question_is_answered_with_citations_the_condition_and_tax_05(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    evidence = Evidence(tax_evidence())
    models = Models(
        intents=("SIDE_QUERY",),
        recommend=("Premiums qualify for a deduction under the old regime only [E1].",),
    )

    t = await follow(held, "How much tax will I save?", models=models, retrieval=evidence)

    # Step 22: the side-query subgraph answers it; the regime is not known, so it is asked, and
    # the bridge leads back to the choices.
    assert ids(t) == [
        "generated:answer",
        "template:side_query_caveat",
        "template:tax_condition",
        "template:regime_ask",
        "registry:DISC-GLOBAL-TAX-05",
        "template:sources",
        "template:side_query_bridge",
        "template:s3_choices",
    ]
    assert [q["action"]["type"] for q in t.quick_replies][:2] == ["FACT", "FACT"]
    assert t.released is not None and t.released.rendered is not None
    assert t.released.rendered.citations == {"E1": "tax:doc:e1:abc123"}
    assert evidence.asked[0][1].focus_uins == [TERM, ROP]
    assert transition(t) == ("S3.STAY", FsmState.S3)


@pytest.mark.asyncio
async def test_an_exclusion_dispute_cites_the_wording_and_offers_the_grievance_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    wording = retrieved(
        [chunk("E1", text="DUMMY: Suicide within twelve months is excluded.")], {"product": 1}
    )
    models = Models(recommend=("The policy wording sets out this exclusion [E1].",))

    t = await follow(
        held,
        "I don't accept the suicide exclusion, take it out",
        models=models,
        retrieval=Evidence(wording),
    )

    assert ids(t) == [
        "generated:answer",
        "template:side_query_caveat",
        "template:exclusion_note",
        "template:sources",
        "template:side_query_bridge",
        "template:s3_choices",
    ]


@pytest.mark.asyncio
async def test_a_guarantee_request_leads_with_no_promise_and_answers_from_the_wording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    wording = retrieved([chunk("E1")], {"product": 1})
    models = Models(
        intents=("OBJECTION_GUARANTEE",),
        recommend=("The policy wording defines the death benefit [E1].",),
    )

    t = await follow(
        held, "can you guarantee my returns", models=models, retrieval=Evidence(wording)
    )

    assert ids(t)[:2] == ["template:guarantee_note", "generated:answer"]


@pytest.mark.asyncio
async def test_a_question_without_sufficient_evidence_abstains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)
    nothing = retrieved([], {}).model_copy(
        update={"abstained": True, "abstain_reason": "NO_SUFFICIENT_EVIDENCE"}
    )

    t = await follow(held, "Is the death benefit paid in instalments?", retrieval=Evidence(nothing))

    assert ids(t) == [
        "template:abstain",
        "template:side_query_caveat",
        "template:side_query_bridge",
        "template:s3_choices",
    ]


@pytest.mark.asyncio
async def test_anything_else_gets_structured_choices(monkeypatch: pytest.MonkeyPatch) -> None:
    held = await presented(monkeypatch)

    t = await follow(held, "hmm okay")

    assert ids(t) == ["template:s3_choices"] and kinds(t) == [
        "APPLY",
        "HUMAN_REQUEST",
        "REVISE",
        "SAVE",
    ]


# --- the rows -------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_save_pauses_with_the_options_kept_and_reengagement_only_with_p3(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch, consent=("P1", "P3"))

    t = await follow(held, action={"type": "SAVE", "payload": {}})

    assert transition(t) == ("S3.2", FsmState.PAUSE)
    assert ids(t) == ["template:paused", "template:s3_summary", "template:reengage"]
    summary = dict(t.released.rendered.parts)["template:s3_summary"]  # type: ignore[union-attr]
    assert (
        "Suraksha Term Shield (999N001V02): cover ₹3,75,00,000 for 26 years, ₹1,06,875 a year"
        in summary
    )


@pytest.mark.asyncio
async def test_discuss_with_my_spouse_pauses_without_reengagement_when_p3_is_not_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)

    t = await follow(held, "I need to discuss with my wife first")

    assert transition(t) == ("S3.2", FsmState.PAUSE)
    assert ids(t) == ["template:paused", "template:s3_summary"]


@pytest.mark.asyncio
async def test_all_rejected_goes_back_to_s2_with_the_rediscovery_framing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = await presented(monkeypatch)

    t = await follow(held, action={"type": "REJECT_ALL", "payload": {}})

    assert transition(t) == ("S3.3", FsmState.S2)
    assert ids(t)[0] == "template:rediscovery" and "template:needs_summary" in ids(t)
    assert t.next.counters["rediscovery_loops"] == 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_rejection_after_two_loops_escalates(monkeypatch: pytest.MonkeyPatch) -> None:
    held = await presented(monkeypatch, counters={"rediscovery_loops": 2})

    t = await follow(held, action={"type": "REJECT_ALL", "payload": {}})

    assert transition(t) == ("S3.3b", FsmState.HUMAN_ESCALATION)
    assert t.transition is not None and t.transition.reason_code == "HE_REDISCOVERY_LIMIT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("loops", "row", "first"), [(0, "S3.STAY", "decline_first"), (1, "S3.1", "declined_exit")]
)
async def test_a_decline_exits_only_after_rediscovery(
    monkeypatch: pytest.MonkeyPatch, loops: int, row: str, first: str
) -> None:
    held = await presented(monkeypatch, counters={"rediscovery_loops": loops})

    t = await follow(held, "no thanks, none of these", models=Models(intents=("DECLINE",)))

    assert transition(t)[0] == row and ids(t)[0] == f"template:{first}"


@pytest.mark.asyncio
async def test_v4_a_needs_correction_in_s3_goes_back_to_s2(monkeypatch: pytest.MonkeyPatch) -> None:
    held = await presented(monkeypatch)
    slot = {
        "slot": "annual_income_inr",
        "value": "3000000",
        "confidence": 0.95,
        "evidence_span": "30 lakh",
    }

    t = await follow(held, "actually my income is 30 lakh", models=Models(slots=(slot,)))

    assert transition(t) == ("G3", FsmState.S2)


# --- logs -----------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_premium_cover_age_or_intake_reaches_the_logs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, restore_logging: None
) -> None:
    with caplog.at_level(logging.DEBUG):
        held = await presented(monkeypatch)
        chosen = await follow(
            held, action={"type": "APPLY", "payload": {"uin": TERM, "rider_uins": ["999A007V01"]}}
        )
        await follow(chosen.next, action=ack_action(chosen))  # type: ignore[arg-type]
    for sentinel in (
        "106875",
        "1,06,875",
        "71250",
        "71,250",
        "37500000",
        "3,75,00,000",
        "signature",
    ):
        assert sentinel not in caplog.text
    assert CITED not in caplog.text
