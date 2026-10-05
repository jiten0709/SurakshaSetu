"""The session schemas (TDD §3.3, §2.6, §3.6, §3.8), copied verbatim, and the graph's state.

Additions to the TDD, marked below: VersionPins.params/ranker/registry (recorded for the audit;
only `rules` is ever sent to the domain tier, which derives params and ranking weights from the
rules release), SessionState.last_prompt_id/focus_uins (the §2.6 turn_router reads them),
SessionState.quote (Quote-Only's last quote, Step 19), and RecommendationPayload.rec_id/shown/
selection/ranking (Step 21).

GraphState is what the LangGraph checkpoint holds. Only the commit node writes it, after the
conv/audit transaction commits; every other node works on the turn's scratch copy (graph/nodes.py).
A failed turn therefore leaves the checkpoint exactly as it was, and no customer text reaches it.
The session is stored as its JSON dump, so the checkpoint serde never deserialises custom types.
"""

import dataclasses
from datetime import date, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from surakshasetu.domain.models import (
    ConsentRecord,
    EligibilityResult,
    NeedsPayload,
    PremiumQuote,
    RankingResult,
    RecommendedOption,
    SuitabilityResult,
)
from surakshasetu.fsm.states import FsmState


class VersionPins(BaseModel):
    prompt_bundle: str  # e.g. "pb-2026.09.2"
    rules: str  # DMN release
    corpus: dict[str, str]  # collection -> snapshot id
    consent_notice: str
    # addition: recorded with every audit event, never sent (Steps 6-7 derive them from `rules`).
    params: str
    ranker: str
    registry: str


class Frame(BaseModel):
    """A side-query (or pause) frame (TDD §2.6)."""

    state: FsmState
    pending_slot: str | None = None
    prompt_id: str | None = None
    focus_uins: list[str] = []


class EligibilityPayload(BaseModel):
    age_years: int = Field(ge=18, le=75)
    dob: date | None = None
    gender: Literal["male", "female", "transgender"] | None = None
    residency: Literal["resident", "nri", "oci_pio"]
    pincode: str = Field(pattern=r"^[1-9][0-9]{5}$")
    tobacco_12m: bool | None  # None = declined to disclose
    occupation_class: str  # occupation master code
    health_flags: dict[str, bool | None]  # screening question id -> answer
    confirmed_at: datetime | None = None
    engine: EligibilityResult | None = None  # eligible UINs, uw_path, reason codes, rules version


class DisclosureAck(BaseModel):
    uin: str
    registry_version: str
    disclosure_set_sha256: str
    document_sha256: dict[str, str]  # "CIS", "BI", "POLICY_WORDING" -> hash shown
    acked_at: datetime


class Shown(BaseModel):
    """addition (Step 21): what a render showed for one option, which its acknowledgment must
    match (V7): the registry set and the documents, by hash."""

    registry_version: str
    set_sha256: str
    documents: dict[str, str]  # "CIS", "BI", "POLICY_WORDING" -> sha256


class Selection(BaseModel):
    """addition (Step 21): the plan the customer chose and the quote it rests on: the option's own,
    or a re-quote of their choice of cover, term, PPT or riders (the gap is then theirs)."""

    uin: str
    quote: PremiumQuote
    protection_gap_inr: str
    customer_choice: bool


class RecommendationPayload(BaseModel):
    options: list[RecommendedOption] = Field(min_length=1, max_length=3)
    ranker_version: str
    suitability_inputs_sha256: str  # must match SuitabilityResult (I2)
    evidence_map: dict[str, str]  # "E3" -> chunk_id, "R2" -> rule id
    rendered_sha256: str  # exact text released
    acks: list[DisclosureAck] = []
    cta: Literal["apply", "advisor", "revise", "save"] | None = None
    # addition (Step 21): the conv.recommendation row and what each option's render showed (both
    # set at the commit), the customer's selection, and the ranking presented (re-quoted options
    # swapped in), which a re-render fills its placeholders from.
    rec_id: UUID | None = None
    shown: dict[str, Shown] = {}
    selection: Selection | None = None
    ranking: RankingResult | None = None


class SessionState(BaseModel):
    session_id: UUID
    subject_ref: str  # pseudonymous; no direct identifiers
    fsm_state: FsmState
    pending_slot: str | None = None
    stack: list[Frame] = []
    consent: ConsentRecord | None = None
    eligibility: EligibilityPayload | None = None
    needs: NeedsPayload | None = None
    suitability: SuitabilityResult | None = None
    recommendation: RecommendationPayload | None = None
    counters: dict[str, int] = {}  # reprompts, off_topic, injection, side_queries
    locale: str = "en-IN"
    pins: VersionPins
    # addition: read by the §2.6 turn_router when it pushes a frame.
    last_prompt_id: str | None = None
    focus_uins: list[str] = []
    # addition (Step 19): the last indicative quote shown in Quote-Only, for the exit summary. Like
    # last_prompt_id it lives in the checkpoint only; hydration drops it, and the summary says less.
    quote: PremiumQuote | None = None


@dataclasses.dataclass
class SlotRow:
    """A slot row a state node wants written in the commit (Steps 19-20): appended to
    conv.slot_value, encrypted, with the session's consent_id (I1)."""

    slot: str
    value: Any
    confidence: float
    status: Literal["proposed", "confirmed", "corrected", "declined"]


class GraphState(BaseModel):
    """The checkpointed state of one session's thread (thread_id = session_id)."""

    session: dict[str, Any] | None = None  # SessionState.model_dump(mode="json")
    committed_seq: int = 0  # the last conv.turn seq this checkpoint reflects
    turn_key: str | None = None  # the Idempotency-Key of the latest invocation (its input)
