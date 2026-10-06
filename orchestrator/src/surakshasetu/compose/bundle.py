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
import unicodedata
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
from surakshasetu.domain.models import Goal, IncomeType, PptOption, ProductType
from surakshasetu.fsm.facts import S0Intent
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
CONSENT_LEXICON = "lexicons/consent_affirmation.yaml"  # Step 18
IDENTITY_LEXICON = "lexicons/identity_question.yaml"
SCREENING_LEXICON = "lexicons/screening.yaml"  # Step 19
NEEDS_LEXICON = "lexicons/needs.yaml"  # Step 20
S3_LEXICON = "lexicons/s3.yaml"  # Step 21
SIDE_QUERY_LEXICON = "lexicons/side_query.yaml"  # Step 22
# Word edges that also hold inside Devanagari (as rails/output.py's lexicon matching).
_START, _END = r"(?<![\wऀ-ॿ])", r"(?![\wऀ-ॿ])"


class BundleError(Exception):
    """The bundle cannot be used. reason: NOT_FOUND, MANIFEST_INVALID, VERSION_MISMATCH,
    FILE_MISSING, FILE_UNLISTED, HASH_MISMATCH, TEMPLATES_INVALID, LEXICON_INVALID, OVER_BUDGET,
    DUMMY_REFUSED,
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
    hint: str | None = None  # Step 19: the format hint after an answer the system could not use


class Labels(_Strict):
    goals: dict[Goal, str]
    ppt: dict[PptOption, str]
    benefit_payment: dict[str, str]
    product_types: dict[ProductType, str]
    documents: dict[Literal["CIS", "BI", "POLICY_WORDING"], str]
    none: str
    or_more: str


class ConsentForm(_Strict):
    """The consent form's labels (TDD §3.5): one checkbox per purpose, the 18+ box, the language
    switch. The notice itself comes from the Consent Service, never from the bundle."""

    purposes: dict[Literal["P1", "P2", "P3"], str]
    adult: str
    submit: str
    retry: str
    languages: dict[Literal["en-IN", "hi-IN"], str]


class Screening(_Strict):
    """State-1 and Quote-Only labels (Step 19): each confirmed value as the read-back states it
    ("{value}", "{district}", "{label}" fields, or a phrase per closed value), short slot names for
    the read-back fix, the closed answers offered as quick replies, and the quick-reply labels."""

    facts: dict[str, str | dict[str, str]]
    names: dict[str, str]
    choices: dict[str, dict[str, str]]
    period: dict[PptOption, str]  # the premium's period by PPT
    range: str  # "from {min} to {max}"
    confirm: str
    fix: str
    retry: str
    advisor: str
    keep_going: str
    opt_in: str
    satisfied: str

    @model_validator(mode="after")
    def _every_fact_named(self) -> Self:
        if unnamed := set(self.facts) - set(self.names):
            raise ValueError(f"read-back facts without a name: {sorted(unnamed)}")
        return self


class NeedsLabels(_Strict):
    """State-2 labels (Step 20): how each needs answer reads in the summary ("{value}" lines; money
    from the slots, existing cover counted from the engine), a declined answer, the defaults the
    short path assumes, short slot names for the summary fix, the closed answers offered as quick
    replies, and the quick-reply labels."""

    facts: dict[str, str]  # slot (or existing_cover_counted) -> "Annual income: {value}"
    declined: dict[str, str]  # slot (or "default") -> how a declined answer reads
    assumed: dict[str, str]  # slot -> how its short-path default reads
    names: dict[str, str]
    income_types: dict[IncomeType, str]
    relations: dict[Literal["spouse", "child", "parent", "other"], str]
    loan_kinds: dict[Literal["home", "vehicle", "education", "personal", "business", "other"], str]
    dependant: str  # "{relation} ({age})"
    loan: str  # "{kind}, {outstanding} outstanding, {years} years left"
    a_year: str  # "{amount} a year"
    none: str
    period: dict[Literal["month", "year"], str]  # the period question's quick replies
    no_one: str
    no_loans: str
    skip: str
    elect: str
    answer_more: str
    see_options: str
    short_yes: str
    short_no: str

    @model_validator(mode="after")
    def _every_fact_named(self) -> Self:
        if unnamed := set(self.facts) - set(self.names) - {"existing_cover_counted"}:
            raise ValueError(f"needs facts without a name: {sorted(unnamed)}")
        if "default" not in self.declined:
            raise ValueError("needs.declined needs a default")
        return self


class SideQueryLabels(_Strict):
    """Step 22's quick-reply labels: the tax regimes (also how a regime reads in a sentence), a
    yes and a no to a proposed fact, and the second-objection offer's save and end."""

    regimes: dict[Literal["old", "new"], str]
    confirm: str
    decline: str
    save: str
    end: str
    keep_going: str


class S3Labels(_Strict):
    """State-3 labels (Step 21): the quick replies, the ranker's reason codes and the alternatives'
    changes as a customer reads them, why a named plan is not an option, and the citation labels
    of the engine facts the model may cite ([R#])."""

    acknowledge: str
    apply_plan: str  # "Apply for {name}"
    cheaper_plan: str  # "Make {name} cheaper"
    alternative: str  # "Choose {n}"
    revisit: str
    reasons: dict[str, str]  # a ranker reason code (or "default") -> a reason line
    changes: dict[Literal["LOWER_COVER", "FEWER_RIDERS", "OTHER_PPT"], str]
    not_recommended: dict[Literal["not_eligible", "not_fit", "default"], str]
    premium_not_shown: str
    engine_option: str  # "{name} ({uin}), ranked {rank}": an option's engine fact
    engine_needs: str  # the needs assessment's engine fact

    @model_validator(mode="after")
    def _a_default_reason(self) -> Self:
        if "default" not in self.reasons:
            raise ValueError("s3.reasons needs a default")
        return self


class Scripts(_Strict):
    readback: str
    money_readback: str
    needs_summary: str
    clarify: str
    ask_to_shorten: str
    abstain: str
    advisor_offer: str
    release_blocked: str  # an output-rail release block (Step 14), before advisor_offer
    handoff: str
    safety: str
    ai_redisclosure: str
    # The cross-cutting handlers (Step 17, pb-2026.10.1 on). Required, so pb-2026.09.1 no longer
    # loads: it was retired by a kill switch, which re-pins its sessions (I7's exception).
    erasure_done: str  # withdrawal recorded (or no consent), data deleted, what is kept and why
    erasure_pending: str  # data deleted, the Consent Service withdrawal still to be recorded
    minor_exit: str  # under 18: the polite exit, nothing kept
    contact_options: str  # no consent, P2 declined, or a closed advisor queue
    advisor_consent_ask: str  # P2, asked before any data reaches an advisor
    paused: str
    # State-0 (Step 18, pb-2026.10.2 on; required, so pb-2026.10.1 no longer loads either).
    greeting: str  # TDD §3.5; the registry's AI disclosure and the notice body follow it
    consent_reprompt: str
    consent_renew: str  # re-entry to S0: the notice changed or the consent lapsed
    consent_retry: str  # the Consent Service or the registry is down: never proceed
    notice_updated: str  # the submitted notice is not the one in force
    age_confirm_ask: str  # the typed path's separate 18+ question
    consent_declined: str  # P1 refused: helpline and branch locator, collect nothing
    intent_ask: str
    consent_form: ConsentForm
    intents: dict[S0Intent, str]  # quick-reply labels
    advisor_contact: dict[Literal["granted", "declined"], str]  # the P2 question's quick replies
    side_query_caveat: dict[str, str]  # fsm state -> caveat
    labels: Labels
    # State-1 and Quote-Only (Step 19, pb-2026.10.3 on; required, so pb-2026.10.2 no longer loads).
    readback_fix: str  # a read-back answered "no": which detail to change
    format_hint: str  # an answer the system could not use, then the slot's hint
    redirect: str  # off-topic: a one-line redirect to the pending question
    reask: str  # the engine's RE_ASK: the question asked once more
    screening_retry: str  # the domain tier is down in S1: retry
    screening_done: str  # eligible: the bridge to S2
    express_offer: str  # a price asked for early (TDD §3.6)
    medical_ack: str  # a serious illness disclosed: no insurability statement
    underwriting_note: str  # "will I be rejected because of ...?": underwriting decides
    nondisclosure_note: str  # "don't tell them I smoke"
    not_eligible: str  # explain, alternatives, helpline; {reason} from not_eligible_reasons
    not_eligible_reasons: dict[str, str]  # an engine reason code (or "default") -> one line
    plan_ask: str
    plan_unknown: str
    plan_unavailable: str  # 409 PRODUCT_WITHDRAWN: withdrawn, expired or not launched
    quote_card: str  # the indicative card, filled from the quote adapter's response
    quote_caveat: str  # the architecture spec's standing Quote-Only caveat
    quote_next: str
    quote_withheld: str  # tobacco declined: no premium, no quote call
    cover_bounds: str  # 422 QUOTE_OUT_OF_BOUNDS on sum_assured_inr
    term_bounds: str  # ... on term_years
    age_bounds: str  # ... on age_years: the plan's entry ages
    rating_unavailable: str  # the rating engine cannot rate the request, or is down
    hard_block: str  # an application asked for in Quote-Only (C13)
    quote_summary: str  # Quote-Only exit
    reengage: str  # with P3 only
    goodbye: str
    screening: Screening
    # State-2 (Step 20, pb-2026.10.4 on; required, so pb-2026.10.3 no longer loads). needs_summary
    # (above) is filled from the slots and the engine's assumptions; never paraphrased.
    amount_readback: str  # a lump sum read back (cover, assets, a loan); money_readback is a year's
    period_ask: str  # "80k" with no period: a month or a year? Never assumed
    summary_assumed: str  # the short path's defaults, on the summary
    summary_implausible: str  # the engine's IMPLAUSIBLE_INPUT: check the figures (soft validation)
    summary_question: str  # the summary's confirmation question, after the lines above
    partial_offer: str  # sufficiency below the minimum: the limitation, and the election
    amber_confirm: str  # amber affordability: explicit confirmation before S3
    no_gap: str  # N <= 0: existing cover appears sufficient; no product (Exit Advisory)
    needs_done: str  # the bridge to S3
    dependency_down: str  # the Suitability Service is down: before `paused`
    needs_retry: str  # ... down while entering S2: retry
    non_earning_basis: str  # homemaker, student, retired or no income: the basis in plain words
    distress_ack: str  # job loss, bereavement, debt: slow down, pause or an advisor
    guarantee_note: str  # "guaranteed high returns": no promise
    competitor_note: str  # another insurer's plan: own products only
    rephrase: str  # comprehension difficulty: the question again, more simply
    short_path_offer: str  # "just tell me the best plan": three questions
    needs: NeedsLabels
    # State-3 (Step 21, pb-2026.10.5 on; required, so pb-2026.10.4 no longer loads). Every number
    # in them comes from the ranker, a quote or the catalog; the hand-off and journey texts are
    # legally significant and stay DUMMY until approved.
    rec_retry: str  # the ranker or the registry is down while S3 is entered: retry
    no_option: str  # the ranker found nothing (NO_ELIGIBLE_OPTION): a careful line, then HE
    rerank_note: str  # a plan shown can no longer be sold: the options again
    requote_note: str  # an indicative premium expired: priced again
    choose_option: str  # "apply" without a plan: which one
    selection_card: str  # the chosen plan and the quote it rests on
    gap_choice: str  # less cover than recommended, chosen by the customer
    ack_ask: str  # read the disclosures and documents shown, then confirm (V7)
    ack_mismatch: str  # an acknowledgment of something not shown: shown again
    ack_recorded: str  # acknowledged without a plan chosen: apply?
    apply_needs_premium: str  # no indicative premium (withheld or unrated): an advisor completes
    handoff_application: str  # the intake taken by the application journey
    journey_down: str  # the intake kept for a retry; an advisor completes it
    alternatives: str  # the heading of the engine's cheaper variants
    alternative_line: str
    cheaper_unavailable: str  # no premium to make cheaper
    which_one: str  # "which should I buy?": the top option and its reasons; the choice is theirs
    not_recommended: str  # a named plan that is not an option, and why
    exclusion_note: str  # an exclusion disputed: the approved wording; an advisor or grievance
    tax_condition: str  # tax: the rules depend on the regime; no personal computation
    revise_ask: str  # "change my answers": which one
    s3_choices: str  # anything else in S3: structured choices only (TDD §3.9)
    s3_summary: str  # need time: the options shown, kept in the session
    s3_summary_line: str
    rediscovery: str  # all rejected: the architecture spec's re-discovery framing, then S2
    decline_first: str  # a decline before any re-discovery: revisit, save or an advisor
    declined_exit: str  # a decline after re-discovery: a graceful exit
    s3: S3Labels
    # Cross-cutting completion (Step 22, pb-2026.10.6 on; required, so pb-2026.10.5 no longer
    # loads). The side-query subgraph's bridge back to the pending question and its offer after
    # five in a row; the tax regime asked, a regime the customer revealed read back, and noted;
    # regulatory pushback (S1's material-fact reason, S2's suitability purpose); the objection
    # handler's early price line, other objections, and the second-time offer of Pause or Exit;
    # the request not to share never-store data. Legally significant ones stay DUMMY.
    side_query_bridge: str
    side_query_offer: str
    regime_ask: str
    regime_confirm: str  # "{regime}"
    regime_noted: str  # "{regime}"
    material_fact_note: str
    suitability_purpose: str
    objection_price_early: str
    objection_other: str
    objection_repeat: str
    pii_reminder: str
    side: SideQueryLabels


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


def phrase(text: str) -> str:
    """How lexicon entries and the text matched against them are compared: NFKC, case-folded,
    spaces collapsed, and final . ! ? । dropped."""
    folded = " ".join(unicodedata.normalize("NFKC", text).casefold().split())
    return folded.rstrip(".!?। ").strip()


def mentions(phrases: frozenset[str], text: str) -> bool:
    """True when the text contains one of the (normalised) phrases as whole words."""
    said = phrase(text)
    return any(re.search(_START + re.escape(p) + _END, said) for p in phrases)


def _phrases(entries: list[str]) -> frozenset[str]:
    # YAML reads a bare yes or no as a boolean: such entries must be quoted.
    if not all(isinstance(e, str) and phrase(e) for e in entries):
        raise ValueError("lexicon entries must be non-empty strings")
    return frozenset(phrase(e) for e in entries)


class ConsentLexicon(_Strict):
    """lexicons/consent_affirmation.yaml: whole-message matches only (TDD §3.5's strict parser)."""

    affirm: frozenset[str]
    decline: frozenset[str]
    adult: frozenset[str]
    minor: frozenset[str]

    @field_validator("affirm", "decline", "adult", "minor", mode="before")
    @classmethod
    def _normalised(cls, entries: list[str]) -> frozenset[str]:
        return _phrases(entries)

    @model_validator(mode="after")
    def _unambiguous(self) -> Self:
        if self.affirm & self.decline or self.adult & self.minor:
            raise ValueError("an entry is both a yes and a no")
        return self


class IdentityLexicon(_Strict):
    """lexicons/identity_question.yaml: phrases matched as whole words anywhere in the turn (I6)."""

    phrases: frozenset[str]

    @field_validator("phrases", mode="before")
    @classmethod
    def _normalised(cls, entries: list[str]) -> frozenset[str]:
        return _phrases(entries)


class ScreeningLexicon(_Strict):
    """lexicons/screening.yaml (Step 19): whole-word phrases anywhere in the turn, like identity."""

    health_terms: frozenset[str]
    concealment: frozenset[str]
    apply: frozenset[str]

    @field_validator("health_terms", "concealment", "apply", mode="before")
    @classmethod
    def _normalised(cls, entries: list[str]) -> frozenset[str]:
        return _phrases(entries)


class NeedsLexicon(_Strict):
    """lexicons/needs.yaml (Step 20): whole-word phrases anywhere in the turn, like screening."""

    short_path: frozenset[str]
    distress: frozenset[str]
    comprehension: frozenset[str]
    guarantee: frozenset[str]
    competitor: frozenset[str]

    @field_validator(
        "short_path", "distress", "comprehension", "guarantee", "competitor", mode="before"
    )
    @classmethod
    def _normalised(cls, entries: list[str]) -> frozenset[str]:
        return _phrases(entries)


class S3Lexicon(_Strict):
    """lexicons/s3.yaml (Step 21): whole-word phrases anywhere in the turn, like needs.yaml."""

    which_one: frozenset[str]
    exclusion_dispute: frozenset[str]
    cheaper: frozenset[str]
    need_time: frozenset[str]

    @field_validator("which_one", "exclusion_dispute", "cheaper", "need_time", mode="before")
    @classmethod
    def _normalised(cls, entries: list[str]) -> frozenset[str]:
        return _phrases(entries)


class SideQueryLexicon(_Strict):
    """lexicons/side_query.yaml (Step 22): whole-word phrases anywhere in the turn. Regulatory
    pushback ("why do you need this?"), a tax regime the customer states, a medical report shared,
    and a request to switch language."""

    pushback: frozenset[str]
    regime_old: frozenset[str]
    regime_new: frozenset[str]
    medical_report: frozenset[str]
    language_hi: frozenset[str]
    language_en: frozenset[str]

    @field_validator(
        "pushback",
        "regime_old",
        "regime_new",
        "medical_report",
        "language_hi",
        "language_en",
        mode="before",
    )
    @classmethod
    def _normalised(cls, entries: list[str]) -> frozenset[str]:
        return _phrases(entries)


@dataclass(frozen=True)
class PromptBundle:
    manifest: Manifest
    sha256: str  # SHA-256(JCS(manifest)): binds every file hash, the budgets and the approvals
    l0: str
    l1: dict[L1Name, str]
    templates: dict[str, Templates]  # locale -> templates
    consent_lexicon: ConsentLexicon
    identity_lexicon: IdentityLexicon
    screening_lexicon: ScreeningLexicon
    needs_lexicon: NeedsLexicon
    s3_lexicon: S3Lexicon
    side_query_lexicon: SideQueryLexicon

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
        CONSENT_LEXICON,
        IDENTITY_LEXICON,
        SCREENING_LEXICON,
        NEEDS_LEXICON,
        S3_LEXICON,
        SIDE_QUERY_LEXICON,
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
    try:
        consent_lexicon = ConsentLexicon.model_validate(yaml.safe_load(files[CONSENT_LEXICON]))
        identity_lexicon = IdentityLexicon.model_validate(yaml.safe_load(files[IDENTITY_LEXICON]))
        screening_lexicon = ScreeningLexicon.model_validate(
            yaml.safe_load(files[SCREENING_LEXICON])
        )
        needs_lexicon = NeedsLexicon.model_validate(yaml.safe_load(files[NEEDS_LEXICON]))
        s3_lexicon = S3Lexicon.model_validate(yaml.safe_load(files[S3_LEXICON]))
        side_query_lexicon = SideQueryLexicon.model_validate(
            yaml.safe_load(files[SIDE_QUERY_LEXICON])
        )
    except (ValidationError, yaml.YAMLError) as exc:
        raise _refuse(version, "LEXICON_INVALID") from exc
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
        consent_lexicon=consent_lexicon,
        identity_lexicon=identity_lexicon,
        screening_lexicon=screening_lexicon,
        needs_lexicon=needs_lexicon,
        s3_lexicon=s3_lexicon,
        side_query_lexicon=side_query_lexicon,
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
