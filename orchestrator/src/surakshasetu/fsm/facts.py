"""Facts: the transition function's only input besides the current state (Step 15).

The orchestrator builds Facts (Step 16's decide node) from orchestrator-set, validated data only:
- consent from the Consent Service's record;
- eligibility and suitability from the domain engines' results;
- selected and acks_valid_for_selected from structured actions the orchestrator has verified
  (DISCLOSURE_ACK against the registry and the document hashes shown);
- counters (rediscovery_loops, low_confidence_streak) from the session.
The model never sets any of these. Turn analysis contributes intents and signals only (withdraw,
human_request, faq, objection, need_time, ...), and only after the input rails.

Every field defaults to "nothing happened". There are no validators, so any Facts pydantic accepts
is a legal input, and transition() is total over them. No field carries personal data: only enums,
flags, counts, hashes and UINs.
"""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from surakshasetu.fsm.states import FsmState

# The fsm may not import surakshasetu.domain, so these mirror the contract's literals;
# tests/property/test_fsm_static.py keeps them equal.
EligibilityOutcome = Literal[
    "ELIGIBLE", "NOT_ELIGIBLE", "HUMAN_ESCALATION", "DATA_ERASURE_EXIT", "RE_ASK"
]
SuitabilityOutcome = Literal["FIT", "NO_GAP", "ESCALATE"]
Affordability = Literal["green", "amber", "red", "unknown"]
# none: no record yet. refused: P1 declined in S0. valid: the Consent Service says the P1 record is
# valid now. lapsed: a record that was valid and is no longer (consent TTL, superseded notice). A
# withdrawal is the `withdraw` signal, honoured in the same turn (I5).
ConsentStatus = Literal["none", "refused", "valid", "lapsed"]
# S0's four quick replies (TDD §3.5). After two failed clarifications the builder sets new_purchase.
S0Intent = Literal["new_purchase", "specific_plan", "existing_policy", "general_faq"]
SHA256_HEX = r"^[0-9a-f]{64}$"


class MandatoryTrigger(StrEnum):
    """V5 plus the injection limit (TDD §3.9). The values are Step 17's escalation reason codes."""

    SELF_HARM = "HE_SAFETY"
    VULNERABLE_COMPLEX = "HE_VULNERABLE_COMPLEX"
    AFFORDABILITY_RED = "HE_AFFORDABILITY_RED"
    OUT_OF_SCOPE = "HE_OUT_OF_SCOPE"
    INJECTION_LIMIT = "HE_INJECTION"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Eligibility(_Frozen):
    """The Eligibility Service's result for the current confirmed eligibility slots. The builder
    drops it (None) when one of those slots changes, so it is never stale."""

    outcome: EligibilityOutcome
    escalation_reason: str | None = None  # HE_NRI, HE_AGE_BAND, HE_COMPLEX_PROPOSER
    eligible_uins: frozenset[str] = frozenset()


class Suitability(_Frozen):
    """The Suitability Service's result. It is current only while inputs_sha256 equals the
    confirmed needs hash (I2), and so are its sufficiency and affordability."""

    inputs_sha256: str = Field(pattern=SHA256_HEX)
    outcome: SuitabilityOutcome
    escalation_reason: str | None = None  # HE_VULNERABLE_COMPLEX, HE_OUT_OF_SCOPE, ...
    profile_sufficiency: float = Field(ge=0, le=1)
    affordability: Affordability


class Selected(_Frozen):
    """The ranked option the customer chose by a structured action."""

    uin: str
    quote_valid: bool  # its quote exists and the IST date is not past valid_until


class Facts(_Frozen):
    consent: ConsentStatus = "none"
    # S0.
    intent: S0Intent | None = None
    # Under 18 (V2): a stated minor age, the 18+ box unticked, or the engine's DATA_ERASURE_EXIT.
    minor: bool = False
    # S1 and Quote-Only.
    eligibility: Eligibility | None = None
    reask_exhausted: bool = False  # RE_ASK again after the one allowed re-ask (Step 19)
    express_path: bool = False
    quote_satisfied: bool = False
    advisory_opt_in: bool = False
    apply_request: bool = False
    # S2.
    suitability: Suitability | None = None
    needs_slots_sha256: str | None = Field(default=None, pattern=SHA256_HEX)
    sufficiency_elected: bool = False  # the recorded election below the threshold (TDD §3.7)
    amber_confirmed: bool = False  # explicit confirmation of amber affordability (TDD §3.7)
    # S3.
    selected: Selected | None = None
    acks_valid_for_selected: bool = False  # every ack matches the set and document hashes (V7)
    no_options: bool = False  # the ranker returned NO_ELIGIBLE_OPTION
    all_rejected: bool = False
    rediscovery_loops: int = Field(default=0, ge=0)  # S3 -> S2 loops taken so far
    # "Need time", "discuss with spouse", and a deferral objection (Step 22's Pause row).
    need_time: bool = False
    explicit_decline: bool = False
    # V4: a confirmed correction of an eligibility or a needs slot, this turn.
    correction: Literal["eligibility", "needs"] | None = None
    paused_from: FsmState | None = None
    # Cross-cutting signals.
    withdraw: bool = False  # META_WITHDRAW or an erasure request
    human_request: bool = False
    frustration: bool = False
    low_confidence_streak: int = Field(default=0, ge=0)  # consecutive turns below the floor
    mandatory_trigger: MandatoryTrigger | None = None
    inactivity_timeout: bool = False  # a timer event, not a customer turn
    faq: bool = False  # FAQ intent at confidence >= 0.7
    objection: bool = False

    @property
    def valid_p1(self) -> bool:
        return self.consent == "valid"

    @property
    def consent_given_ever(self) -> bool:
        return self.consent in ("valid", "lapsed")

    @property
    def eligible(self) -> bool:
        return self.eligibility is not None and self.eligibility.outcome == "ELIGIBLE"

    @property
    def suitability_current(self) -> bool:
        return (
            self.suitability is not None
            and self.suitability.inputs_sha256 == self.needs_slots_sha256
        )

    @property
    def current_suitability(self) -> Suitability | None:
        """The suitability record when it is current (I2), else None."""
        return self.suitability if self.suitability_current else None

    @property
    def over_insured(self) -> bool:
        current = self.current_suitability
        return current is not None and current.outcome == "NO_GAP"
