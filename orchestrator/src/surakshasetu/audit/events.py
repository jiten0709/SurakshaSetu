"""Audit event types (TDD §4.3) and the header model each one carries.

A header holds only non-personal, queryable fields: ids, versions, hashes, codes, scores and
counts. It is stored in clear and hashed into the chain. Anything personal (text, slot values,
outputs about the customer, staff identities) goes in the payload, which is always encrypted under
the subject's DEK. tests/unit/test_header_models.py fails the build on a field name that looks
personal. Later steps may add fields: verification rehashes the stored header, not the model.
"""

from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

Sha256Hex = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class EventType(StrEnum):
    CONSENT_CAPTURED = "CONSENT_CAPTURED"
    CONSENT_WITHDRAWN = "CONSENT_WITHDRAWN"
    TURN_INPUT = "TURN_INPUT"
    GUARD_VERDICT = "GUARD_VERDICT"
    MODEL_CALL = "MODEL_CALL"
    RETRIEVAL = "RETRIEVAL"
    ENGINE_DECISION = "ENGINE_DECISION"
    STATE_TRANSITION = "STATE_TRANSITION"
    RESPONSE_RELEASED = "RESPONSE_RELEASED"
    DISCLOSURE_ACK = "DISCLOSURE_ACK"
    HANDOFF = "HANDOFF"
    ERASURE_REQUEST = "ERASURE_REQUEST"
    KILL_SWITCH = "KILL_SWITCH"
    CONFIG_RELEASE = "CONFIG_RELEASE"
    SUFFICIENCY_ELECTION = "SUFFICIENCY_ELECTION"


class Header(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConsentCapturedHeader(Header):
    consent_id: UUID
    notice_version: str
    notice_sha256: Sha256Hex
    purposes: list[Literal["P1", "P2", "P3"]]
    method: Literal["structured_action", "parsed_affirmation"]


class ConsentWithdrawnHeader(Header):
    consent_id: UUID
    purposes: list[Literal["P1", "P2", "P3"]]


class TurnInputHeader(Header):
    turn_id: UUID
    turn_seq: int
    language: str
    channel: Literal["web", "app"]
    turn_key: UUID  # the idempotency key


class GuardVerdictHeader(Header):
    rail: str
    rule_id: str
    score: float | None = None
    action: str


class ModelCallHeader(Header):
    route: str
    served_model: str
    envelope_sha256: Sha256Hex
    tokens_in: int
    tokens_out: int
    latency_ms: int
    fallback_hops: int = 0


class RetrievalHeader(Header):
    collections: list[str]
    snapshot_ids: list[str]
    chunk_ids: list[str]
    rerank_scores: list[float]
    abstained: bool


class EngineDecisionHeader(Header):
    service: str
    decision_id: str
    rules_version: str | None = None
    params_version: str | None = None
    inputs_sha256: Sha256Hex
    reason_codes: list[str]


class StateTransitionHeader(Header):
    from_state: str
    to_state: str
    trigger: str  # the matched row id
    invariants: dict[str, bool]


class ResponseReleasedHeader(Header):
    turn_id: UUID
    rendered_sha256: Sha256Hex
    citations: list[str]
    verdicts: dict[str, str]
    disclosure_set_sha256s: list[Sha256Hex]


class DisclosureAckHeader(Header):
    uin: str
    registry_version: str
    set_sha256: Sha256Hex
    document_sha256s: list[Sha256Hex]


class HandoffHeader(Header):
    handoff_id: UUID
    reason_code: str
    queue: str


class ErasureRequestHeader(Header):
    reason_code: str


# Approver identities are staff personal data: they go in the payload, under the system key.
class KillSwitchHeader(Header):
    target_kind: Literal["product", "prompt_bundle", "route"]
    target: str
    active: bool
    reason_code: str
    approvals_count: int


class ConfigReleaseHeader(Header):
    artefact: str
    version: str
    sha256: Sha256Hex
    approvals_count: int


class SufficiencyElectionHeader(Header):
    score: float
    threshold: float
    missing_slot_count: int
    elected: bool


HEADERS: dict[EventType, type[Header]] = {
    EventType.CONSENT_CAPTURED: ConsentCapturedHeader,
    EventType.CONSENT_WITHDRAWN: ConsentWithdrawnHeader,
    EventType.TURN_INPUT: TurnInputHeader,
    EventType.GUARD_VERDICT: GuardVerdictHeader,
    EventType.MODEL_CALL: ModelCallHeader,
    EventType.RETRIEVAL: RetrievalHeader,
    EventType.ENGINE_DECISION: EngineDecisionHeader,
    EventType.STATE_TRANSITION: StateTransitionHeader,
    EventType.RESPONSE_RELEASED: ResponseReleasedHeader,
    EventType.DISCLOSURE_ACK: DisclosureAckHeader,
    EventType.HANDOFF: HandoffHeader,
    EventType.ERASURE_REQUEST: ErasureRequestHeader,
    EventType.KILL_SWITCH: KillSwitchHeader,
    EventType.CONFIG_RELEASE: ConfigReleaseHeader,
    EventType.SUFFICIENCY_ELECTION: SufficiencyElectionHeader,
}
