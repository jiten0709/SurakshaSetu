"""The prompt bundle (TDD §3.4, §4.5): L0 constitution, L1 state instructions and the templates for
every legally significant or fixed text, versioned in Git, approved and pinned per session.

content/prompt-bundles/<version>/manifest.yaml lists every file with the SHA-256 of its bytes. The
loader refuses a bundle whose files differ from the manifest in any way (missing, unlisted or
changed), and a DUMMY bundle in pilot or prod. A released bundle is immutable: changed content
needs a new version, and activate() refuses a recorded version whose hash moved.

I7: a session keeps its pinned bundle. The one exception is a kill switch on that bundle, which
re-pins the session to the active bundle (decided 2026-10-01); a pinned bundle that is missing or
fails its hashes without a kill switch is refused, never silently swapped.
"""

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.chain import SYSTEM_SESSION, Conn
from surakshasetu.audit.events import ConfigReleaseHeader, EventType, Sha256Hex
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.crypto.keys import SYSTEM_KEY_REF, KeyService
from surakshasetu.domain.models import Goal, PptOption, ProductType
from surakshasetu.gateway import Route
from surakshasetu.kb.chunker import count_tokens

logger = logging.getLogger(__name__)

PROMPT_BUNDLES = Path(__file__).resolve().parents[4] / "content" / "prompt-bundles"
_VERSION = re.compile(r"^pb-\d{4}\.\d{2}\.\d+$")

L1Name = Literal["S1", "S2", "S3", "side-query", "summarise"]
# Each L1 layer belongs to one route, so a state's instructions never reach another route.
L1_ROUTES: dict[L1Name, Route] = {
    "S1": Route.GEN_CONVERSE,
    "S2": Route.GEN_CONVERSE,
    "S3": Route.GEN_RECOMMEND,
    "side-query": Route.GEN_RECOMMEND,
    "summarise": Route.SUMMARISE,
}
LOCALES = ("en-IN", "hi-IN")
L0_PATH = "l0/constitution.txt"
TEMPLATE_FILES = ("slots", "scripts", "recommendation")


class BundleError(Exception):
    """The bundle cannot be used. reason: NOT_FOUND, MANIFEST_INVALID, VERSION_MISMATCH,
    FILE_MISSING, FILE_UNLISTED, HASH_MISMATCH, TEMPLATES_INVALID, OVER_BUDGET, DUMMY_REFUSED,
    KILL_SWITCHED (the kill-switched bundle is also the active one) or RELEASED_WITH_OTHER_HASH."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Approval(_Strict):
    role: str
    by: str  # a staff identity: CONFIG_RELEASE carries it in the encrypted payload only
    at: date


class Budgets(_Strict):
    """TDD §1.5's envelope budgets, in tokens."""

    constitution: int = Field(gt=0)
    state: int = Field(gt=0)
    facts: int = Field(gt=0)
    summary: int = Field(gt=0)
    recent_turns: int = Field(gt=0)
    evidence: int = Field(gt=0)
    user_turn: int = Field(gt=0)


class Manifest(_Strict):
    version: str
    is_dummy: bool
    insurer: str  # fills {insurer} in L0 and the templates
    approved_by: list[Approval]
    budgets: Budgets
    summary_every_turns: int = Field(ge=1)  # the rolling summary's regeneration cadence
    files: dict[str, Sha256Hex]  # every file but the manifest, path -> SHA-256 of its bytes

    @field_validator("approved_by")
    @classmethod
    def _two_person_sign_off(cls, approvals: list[Approval]) -> list[Approval]:
        if len({a.by for a in approvals}) < 2:  # TDD §4.5
            raise ValueError("a prompt bundle needs two distinct approvers")
        return approvals


class SlotTemplate(_Strict):
    question: str
    reason: str  # the approved one-line reason


class Labels(_Strict):
    goals: dict[Goal, str]
    ppt: dict[PptOption, str]
    benefit_payment: dict[str, str]
    product_types: dict[ProductType, str]
    documents: dict[Literal["CIS", "BI", "POLICY_WORDING"], str]
    none: str
    or_more: str


class Scripts(_Strict):
    readback: str
    money_readback: str
    needs_summary: str
    clarify: str
    ask_to_shorten: str
    abstain: str
    advisor_offer: str
    handoff: str
    safety: str
    ai_redisclosure: str
    side_query_caveat: dict[str, str]  # fsm state -> caveat
    labels: Labels


Attribute = Literal[
    "category",
    "entry_age",
    "maturity_age",
    "cover_range",
    "term_range",
    "ppt_options",
    "benefit_payment",
    "riders",
]


class Comparison(_Strict):
    heading: str
    attributes: dict[ProductType, list[Attribute]]  # the fixed row set per product category
    rows: dict[Attribute, str]  # row labels

    @model_validator(mode="after")
    def _every_row_labelled(self) -> Self:
        if unlabelled := {a for rows in self.attributes.values() for a in rows} - set(self.rows):
            raise ValueError(f"comparison rows without a label: {sorted(unlabelled)}")
        return self


class Cta(_Strict):
    """Four equal choices: no default, no urgency (TDD §3.8)."""

    heading: str
    apply: str
    advisor: str
    revise: str
    save: str


class Recommendation(_Strict):
    needs_recap: str
    partial_profile: str
    option_card: str
    premium: str
    premium_single: str
    premium_withheld: str
    document: str
    comparison: Comparison
    deterministic_card: str
    disclosures_heading: str
    cta: Cta
    sources_heading: str
    source_line: str


class Templates(_Strict):
    slots: dict[str, SlotTemplate]  # reason_line_id -> question
    scripts: Scripts
    recommendation: Recommendation


@dataclass(frozen=True)
class PromptBundle:
    manifest: Manifest
    sha256: str  # SHA-256(JCS(manifest)): binds every file hash, the budgets and the approvals
    l0: str
    l1: dict[L1Name, str]
    templates: dict[str, Templates]  # locale -> templates

    @property
    def version(self) -> str:
        return self.manifest.version


def load_bundle(version: str, *, env: str, root: Path = PROMPT_BUNDLES) -> PromptBundle:
    base = root / version
    manifest_path = base / "manifest.yaml"
    if not _VERSION.match(version) or not manifest_path.is_file():
        raise _refuse(version, "NOT_FOUND")
    try:
        manifest = Manifest.model_validate(yaml.safe_load(manifest_path.read_bytes()))
    except (ValidationError, yaml.YAMLError) as exc:
        raise _refuse(version, "MANIFEST_INVALID") from exc
    if manifest.version != version:
        raise _refuse(version, "VERSION_MISMATCH")
    present = {
        p.relative_to(base).as_posix()
        for p in base.rglob("*")
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(base).parts)
    } - {"manifest.yaml"}
    required = {
        L0_PATH,
        *(f"l1/{name}.txt" for name in L1_ROUTES),
        *(f"templates/{loc}/{name}.yaml" for loc in LOCALES for name in TEMPLATE_FILES),
    }
    if missing := sorted((set(manifest.files) | required) - present):
        raise _refuse(version, "FILE_MISSING", missing[0])
    if unlisted := sorted(present - set(manifest.files)):
        raise _refuse(version, "FILE_UNLISTED", unlisted[0])
    files = {path: (base / path).read_bytes() for path in sorted(manifest.files)}
    for path, content in files.items():
        if hashlib.sha256(content).hexdigest() != manifest.files[path]:
            raise _refuse(version, "HASH_MISMATCH", path)
    if manifest.is_dummy and env in ("pilot", "prod"):
        raise _refuse(version, "DUMMY_REFUSED")
    try:
        templates = {
            loc: Templates.model_validate(
                {n: yaml.safe_load(files[f"templates/{loc}/{n}.yaml"]) for n in TEMPLATE_FILES}
            )
            for loc in LOCALES
        }
    except (ValidationError, yaml.YAMLError) as exc:
        raise _refuse(version, "TEMPLATES_INVALID") from exc
    l0 = files[L0_PATH].decode("utf-8")
    l1: dict[L1Name, str] = {name: files[f"l1/{name}.txt"].decode("utf-8") for name in L1_ROUTES}
    budgets = manifest.budgets
    if count_tokens(l0) > budgets.constitution:
        raise _refuse(version, "OVER_BUDGET", L0_PATH)
    if over := [name for name, text in l1.items() if count_tokens(text) > budgets.state]:
        raise _refuse(version, "OVER_BUDGET", f"l1/{over[0]}.txt")
    bundle = PromptBundle(
        manifest=manifest,
        sha256=sha256_hex(manifest.model_dump(mode="json")),
        l0=l0,
        l1=l1,
        templates=templates,
    )
    logger.info("prompt bundle %s loaded: %d files", version, len(files))
    return bundle


def load_pinned(
    pinned: str, *, active: str, kill_switched: bool, env: str, root: Path = PROMPT_BUNDLES
) -> PromptBundle:
    """The session's bundle. A kill switch on the pinned version is the only way off it (I7): the
    active bundle comes back, and the caller records the re-pin. Otherwise a BundleError stands."""
    if kill_switched:
        if active == pinned:  # nothing to re-pin to
            raise _refuse(pinned, "KILL_SWITCHED")
        logger.warning("prompt bundle %s is kill-switched: re-pinning to %s", pinned, active)
        return load_bundle(active, env=env, root=root)
    return load_bundle(pinned, env=env, root=root)


def activate(conn: Conn, keys: KeyService, bundle: PromptBundle) -> bool:
    """Record the bundle's release as a CONFIG_RELEASE event on the system chain, in the caller's
    transaction (it never commits). True when written, False when this exact release is already
    recorded. A recorded version with another hash is refused: released content never changes."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s::text, 0))", (SYSTEM_SESSION,))
    recorded = {
        row[0]
        for row in conn.execute(
            "SELECT header->>'sha256' FROM audit.audit_event WHERE session_id = %s"
            " AND event_type = %s AND header->>'artefact' = %s AND header->>'version' = %s",
            (SYSTEM_SESSION, EventType.CONFIG_RELEASE.value, "prompt_bundle", bundle.version),
        ).fetchall()
    }
    if recorded - {bundle.sha256}:
        raise _refuse(bundle.version, "RELEASED_WITH_OTHER_HASH")
    if recorded:
        return False
    manifest = bundle.manifest
    audit_chain.append(
        conn,
        keys,
        session_id=SYSTEM_SESSION,
        event_type=EventType.CONFIG_RELEASE,
        fsm_state="SYSTEM",
        pins={},
        header=ConfigReleaseHeader(
            artefact="prompt_bundle",
            version=bundle.version,
            sha256=bundle.sha256,
            approvals_count=len(manifest.approved_by),
        ),
        payload={
            "approved_by": [a.model_dump(mode="json") for a in manifest.approved_by],
            "files": manifest.files,
        },
        key_ref=SYSTEM_KEY_REF,
    )
    logger.info("prompt bundle %s released", bundle.version)
    return True


def _refuse(version: str, reason: str, path: str = "") -> BundleError:
    logger.error("prompt bundle %s refused: %s %s", version, reason, path)
    return BundleError(reason)
