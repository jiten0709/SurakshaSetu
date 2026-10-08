"""Output rails 6-8 (TDD §4.2) and the regenerate-then-card policy (TDD §3.8, §1.5).

    6 lexicon    the versioned EN/HI/Hinglish pack (content/lexicon/), per sentence
    7 grounding  products (I3), placeholders, numbers, citation handles, factual sentences cited,
                 then claim-level NLI through verify-claims
    8 release    disclosure-set hashes (I4), guard-input in output mode, cross-session leaks, DUMMY

Rails 6-7 read only the model's text, never L0/L1, a template or registry text; rail 8 reads the
whole rendered turn. A lexicon or grounding failure regenerates once with the error list. A second
failure, or a verifier that cannot run, falls back to render(None): the deterministic card in S3,
the template in S1-S2. A release-check failure blocks the release: a template and an advisor offer.
The rails never edit model text. They pass it, regenerate it, or replace it.

Every verdict is one GUARD_VERDICT event, in this fixed order, in the caller's transaction (I8 is
the caller's: nothing is released before the turn commits). Nothing here logs text.
"""

import asyncio
import hashlib
import html
import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from surakshasetu.analysis.normalisers import devanagari_digits_to_ascii
from surakshasetu.audit import chain as audit_chain
from surakshasetu.audit.chain import Conn
from surakshasetu.audit.events import EventType, GuardVerdictHeader
from surakshasetu.compose.bundle import PromptBundle
from surakshasetu.compose.citations import TurnHandles, cited
from surakshasetu.compose.composer import Rendered
from surakshasetu.compose.placeholders import PlaceholderError, fill
from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.crypto.keys import KeyService
from surakshasetu.domain.models import DisclosureSet, RankingResult, SuitabilityResult
from surakshasetu.gateway import DataClass, Gateway, GatewayUnavailable, Route
from surakshasetu.kb.payload import Collection, content_sha256
from surakshasetu.rails import redact
from surakshasetu.rails.injection import call_guard
from surakshasetu.rails.normalise import normalise

logger = logging.getLogger(__name__)

LEXICONS = Path(__file__).resolve().parents[4] / "content" / "lexicon"
_VERSION = re.compile(r"^\d{4}\.\d{2}\.\d+$")
_ACTION = re.compile(r"block|require_citation\(domain=(regulatory|product|tax)\)")
_ALLOW_IF = re.compile(r"cites_clause_type\((\w+)\)")

# Word edges that also hold inside Devanagari, where \b breaks at every vowel sign and virama.
_START, _END = r"(?<![\wऀ-ॿ])", r"(?![\wऀ-ॿ])"
_NEVER = re.compile(r"(?!)")
_CITATION = re.compile(r"\[[ER]\d+(?:\s*,\s*[ER]\d+)*\]")
_LEADING_CITATIONS = re.compile(r"^(?:\[[ER]\d+(?:\s*,\s*[ER]\d+)*\]\s*)+")
_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)[^}]*\}\}")
_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+|\n+")
_UIN = re.compile(r"\b\d{3}[NA]\d{3}V\d{2}\b")
_NUMERAL = re.compile(r"\d+(?:[.,]\d+)*")
_TOKEN = re.compile(r"(?:<|&lt;)(?:" + "|".join(redact.ENTITIES) + r")_\d+(?:>|&gt;)")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)

Rail = Literal["lexicon", "grounding", "release"]


class LexiconError(Exception):
    """The pack cannot be used. reason: NOT_FOUND, INVALID, VERSION_MISMATCH, DUPLICATE_ID,
    BAD_PATTERN, UNKNOWN_ACTION or UNKNOWN_CLAUSE_TYPE."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _RuleSpec(_Strict):
    id: str = Field(pattern=r"^LX-[A-Z]+-\d{2}$")
    pattern: str
    action: str
    allow_if: str | None = None


class _ClauseTypeSpec(_Strict):
    domain: Collection
    doc_types: list[str] = Field(min_length=1)
    section: str  # matched against the chunk's own heading, section_path[-1]
    text: str


class _PackSpec(_Strict):
    pack: Literal["output-lexicon"]
    version: str
    owner: str
    rules: list[_RuleSpec] = Field(min_length=1)
    clause_types: dict[str, _ClauseTypeSpec] = {}
    factual_terms: dict[Collection, list[str]] = {}
    number_words: list[str] = []


@dataclass(frozen=True)
class Rule:
    id: str
    pattern: re.Pattern[str]
    require_domain: Collection | None  # require_citation(domain=…); None: block
    allow_if: str | None  # a clause type that, cited in the same sentence, allows the match


@dataclass(frozen=True)
class ClauseType:
    domain: Collection
    doc_types: frozenset[str]
    section: re.Pattern[str]
    text: re.Pattern[str]


@dataclass(frozen=True)
class LexiconPack:
    version: str
    rules: tuple[Rule, ...]
    clause_types: Mapping[str, ClauseType]
    factual_terms: re.Pattern[str]  # any domain's term makes a sentence factual
    number_words: re.Pattern[str]


def load_pack(version: str, *, root: Path = LEXICONS) -> LexiconPack:
    path = root / f"output-lexicon-{version}.yaml"
    if not _VERSION.match(version) or not path.is_file():
        raise _refuse(version, "NOT_FOUND")
    try:
        spec = _PackSpec.model_validate(yaml.safe_load(path.read_bytes()))
    except (ValidationError, yaml.YAMLError) as exc:
        raise _refuse(version, "INVALID") from exc
    if spec.version != version:
        raise _refuse(version, "VERSION_MISMATCH")
    if len({r.id for r in spec.rules}) != len(spec.rules):
        raise _refuse(version, "DUPLICATE_ID")
    try:
        clause_types = {
            name: ClauseType(
                c.domain, frozenset(c.doc_types), _compile(c.section), _compile(c.text)
            )
            for name, c in spec.clause_types.items()
        }
        rules = tuple(_rule(version, r, clause_types) for r in spec.rules)
        pack = LexiconPack(
            version=version,
            rules=rules,
            clause_types=clause_types,
            factual_terms=_words([t for terms in spec.factual_terms.values() for t in terms]),
            number_words=_words(spec.number_words),
        )
    except re.error as exc:
        raise _refuse(version, "BAD_PATTERN") from exc
    logger.info("output lexicon %s loaded: %d rules", version, len(rules))
    return pack


def _rule(version: str, spec: _RuleSpec, clause_types: Mapping[str, ClauseType]) -> Rule:
    action = _ACTION.fullmatch(spec.action)
    if action is None:
        raise _refuse(version, "UNKNOWN_ACTION")
    allow_if = None
    if spec.allow_if is not None:
        clause = _ALLOW_IF.fullmatch(spec.allow_if)
        if clause is None or clause.group(1) not in clause_types:
            raise _refuse(version, "UNKNOWN_CLAUSE_TYPE")
        allow_if = clause.group(1)
    domain = cast(Collection | None, action.group(1))
    return Rule(spec.id, _compile(spec.pattern), domain, allow_if)


def _compile(pattern: str) -> re.Pattern[str]:
    """NFKC, as the scanned text is, so a Devanagari nukta matches in either form."""
    return re.compile(unicodedata.normalize("NFKC", pattern))


def _words(words: Sequence[str]) -> re.Pattern[str]:
    if not words:
        return _NEVER
    alternatives = sorted(
        (r"\s+".join(re.escape(part) for part in unicodedata.normalize("NFKC", w).split()))
        for w in words
    )
    alternatives.sort(key=len, reverse=True)  # the longest alternative wins at a position
    return re.compile(f"{_START}(?:{'|'.join(alternatives)}){_END}", re.IGNORECASE)


def _refuse(version: str, reason: str) -> LexiconError:
    logger.error("output lexicon %s refused: %s", version, reason)
    return LexiconError(reason)


@dataclass(frozen=True)
class OutputContext:
    session_id: UUID
    turn_id: UUID
    subject_ref: UUID
    fsm_state: str
    pins: Mapping[str, Any]
    key_ref: str
    locale: str
    route: Route  # the generation route: gen-recommend verifies every claim, gen-converse samples
    handles: TurnHandles  # Envelope.handles: the handles the model saw, the only citable ones
    customer_text: str  # the turn's redacted text, for the guard's output mode
    products: Mapping[str, str] = field(default_factory=dict)  # base product UIN -> catalog name
    customer_uins: frozenset[str] = frozenset()  # products the customer named (I3)
    own_pii: frozenset[str] = frozenset()  # this session's own values, which a release may show
    # Approved text shown verbatim outside a disclosure set (Step 18): the consent notice and the
    # registry's AI disclosure. Contact details in them (a grievance officer's email) are not leaks.
    approved_text: tuple[str, ...] = ()


@dataclass(frozen=True)
class Numbers:
    """Where placeholders are filled from (S3). Without it every placeholder is refused."""

    ranking: RankingResult
    suitability: SuitabilityResult


@dataclass(frozen=True)
class Verdict:
    rail: Rail
    rule_id: str
    # pass | allow | fail | unavailable | not_sampled | skipped | block | count; the policy turns
    # fail and unavailable into regenerate or fallback before auditing.
    action: str
    score: float | None = None
    detail: tuple[str, ...] = ()  # error-list lines: rule ids, handles, the model's own words


@dataclass(frozen=True)
class Released:
    kind: Literal["narrative", "regenerated", "fallback", "blocked"]
    text: str  # exactly what to release
    rendered: Rendered | None  # None when blocked
    verdicts: dict[str, str]  # "<rail>:<rule id>" -> final action, for RESPONSE_RELEASED


@dataclass(frozen=True)
class Sentence:
    n: int  # 1-based, as the error list names it
    text: str
    handles: list[str]


def sentences(text: str) -> list[Sentence]:
    """Split on . ! ? । and newlines; a citation that opens a sentence ("fact. [E3]") belongs to
    the one before. ponytail: abbreviations ("Rs. 500") split early; the citation rule then asks
    for a handle on each half, which fails closed."""
    parts: list[str] = []
    for part in _SENTENCE_END.split(text):
        part = part.strip()
        lead = _LEADING_CITATIONS.match(part)
        if lead and parts:
            parts[-1] = f"{parts[-1]} {lead.group(0).strip()}"
            part = part[lead.end() :].strip()
        if part:
            parts.append(part)
    return [Sentence(n, s, cited(s)) for n, s in enumerate(parts, start=1)]


def lexicon(text: str, ctx: OutputContext, pack: LexiconPack) -> list[Verdict]:
    """Rail 6: one verdict per rule that matched (allow or fail), or one pass."""
    hits: dict[str, list[str]] = {}
    for s in sentences(normalise(text).text):
        chunks = [ctx.handles.evidence[h] for h in s.handles if h in ctx.handles.evidence]
        for rule in pack.rules:
            match = rule.pattern.search(s.text)
            if match is None:
                continue
            problems = hits.setdefault(rule.id, [])
            said = f'{rule.id} sentence {s.n}: "{match.group(0)}"'
            if rule.require_domain is not None:
                if not any(c.domain == rule.require_domain for c in chunks):
                    problems.append(f"{said} needs a citation of {rule.require_domain} evidence")
            elif rule.allow_if is not None:
                clause = pack.clause_types[rule.allow_if]
                if not any(
                    _is_clause(c.domain, c.doc_type, c.section_path, c.text, clause) for c in chunks
                ):
                    problems.append(f"{said} is allowed only citing a {rule.allow_if} clause")
            else:
                problems.append(f"{said} is not allowed")
    verdicts = [
        Verdict("lexicon", rule_id, "fail" if problems else "allow", detail=tuple(problems))
        for rule_id, problems in hits.items()
    ]
    return verdicts or [Verdict("lexicon", "none", "pass")]


def _is_clause(
    domain: str, doc_type: str, section_path: Sequence[str], text: str, clause: ClauseType
) -> bool:
    return (
        domain == clause.domain
        and doc_type in clause.doc_types
        and bool(clause.section.search(section_path[-1]))
        and bool(clause.text.search(text))
    )


def grounding(
    text: str, ctx: OutputContext, pack: LexiconPack, numbers: Numbers | None
) -> list[Verdict]:
    """Rail 7's deterministic checks, one verdict each. The placeholders and handles are checked
    on the raw text, as the composer will fill and render it; the rest on the normalised text."""
    normalised = normalise(text).text
    split = sentences(normalised)
    return [
        _products(split, ctx),
        _placeholders(text, numbers),
        _numbers(split, ctx, pack),
        _handles(text, normalised, ctx),
        _citations(split, ctx, pack),
    ]


def _products(sentences: Sequence[Sentence], ctx: OutputContext) -> Verdict:
    """I3: before S3 only products the customer named; in S3 also those in ENGINE_RESULT. Rider
    names ("Critical illness") are benefit types, so riders are checked by UIN only."""
    allowed = set(ctx.customer_uins)
    if ctx.fsm_state == "S3":
        allowed |= {
            uin
            for f in ctx.handles.engine.values()
            for uin in _UIN.findall(f"{json.dumps(f.content)} {f.rule} {f.label}")
        }
    problems = []
    for s in sentences:
        if products_named(s.text, ctx.products) - allowed:
            problems.append(
                f"GR-PRODUCT sentence {s.n}: it names a product that is not in ENGINE_RESULT"
            )
    return _verdict("GR-PRODUCT", problems)


def products_named(text: str, products: Mapping[str, str]) -> set[str]:
    """The UINs a text names: UINs as written, and base products by catalog name (UIN -> name),
    longest name first, so "… Shield ROP" is not also "… Shield". Also Quote-Only's plan detection
    (Step 19), on the customer's normalised turn."""
    scan, named = text, set(_UIN.findall(text))
    for uin, name in sorted(products.items(), key=lambda kv: len(kv[1]), reverse=True):
        pattern = _words([name])
        if pattern.search(scan):
            named.add(uin)
            scan = pattern.sub(" ", scan)
    return named


def _placeholders(raw: str, numbers: Numbers | None) -> Verdict:
    problems = []
    for s in sentences(raw):
        if numbers is None:
            if "{" in s.text or "}" in s.text:
                problems.append(f"GR-PLACEHOLDER sentence {s.n}: write no placeholders here")
            continue
        try:
            fill(s.text, numbers.ranking, numbers.suitability)
        except PlaceholderError as exc:
            problems.append(f"GR-PLACEHOLDER sentence {s.n}: {exc.reason}")
    return _verdict("GR-PLACEHOLDER", problems)


def _numbers(sentences: Sequence[Sentence], ctx: OutputContext, pack: LexiconPack) -> Verdict:
    """After the fill, every number comes from the engine or is verbatim in a cited source: so a
    number the model wrote must appear in a source its own sentence cites."""
    problems = []
    for s in sentences:
        found = _number_tokens(s.text, pack)
        if not found:
            continue
        cited_sources = sources(s.handles, ctx.handles)
        for token in dict.fromkeys(found):
            pattern = (
                re.compile(rf"(?<!\d)(?<!\d[.,]){re.escape(token)}(?!\d)(?![.,]\d)")
                if token[0].isdigit()
                else _words([token])
            )
            if not any(pattern.search(source) for source in cited_sources):
                problems.append(
                    f'GR-NUMBER sentence {s.n}: "{token}" is not in ENGINE_RESULT or in the'
                    " evidence this sentence cites"
                )
    return _verdict("GR-NUMBER", problems)


def _number_tokens(text: str, pack: LexiconPack) -> list[str]:
    """Numerals (any script) and number words, ignoring placeholders, handles and UINs."""
    bare = devanagari_digits_to_ascii(
        _UIN.sub(" ", _CITATION.sub(" ", _PLACEHOLDER.sub(" ", text)))
    )
    return _NUMERAL.findall(bare) + [m.group(0) for m in pack.number_words.finditer(bare)]


def sources(cited_handles: Sequence[str], handles: TurnHandles) -> list[str]:
    sources = []
    for h in cited_handles:
        if h in handles.evidence:
            sources.append(devanagari_digits_to_ascii(normalise(handles.evidence[h].text).text))
        elif h in handles.engine:
            f = handles.engine[h]
            content = json.dumps(f.content, ensure_ascii=False, sort_keys=True)
            sources.append(f"{f.rule} {f.label} {content}")
    return sources


def _handles(raw: str, normalised: str, ctx: OutputContext) -> Verdict:
    issued = set(ctx.handles.evidence) | set(ctx.handles.engine)
    in_raw = cited(raw)
    problems = [
        f"GR-HANDLE: [{h}] was not issued this turn"
        for h in dict.fromkeys(in_raw)
        if h not in issued
    ]
    if Counter(in_raw) != Counter(cited(normalised)):  # e.g. a zero-width character in a handle
        problems.append("GR-HANDLE: a citation is malformed; write handles exactly as [E1] or [R1]")
    return _verdict("GR-HANDLE", problems)


def _citations(sentences: Sequence[Sentence], ctx: OutputContext, pack: LexiconPack) -> Verdict:
    """TDD §2.5: a sentence stating a product, regulatory or tax fact (a lexicon term), a number
    or a placeholder carries at least one handle issued this turn."""
    issued = set(ctx.handles.evidence) | set(ctx.handles.engine)
    problems = [
        f"GR-CITATION sentence {s.n}: it states a product, regulatory or tax fact or a number"
        " without a handle"
        for s in sentences
        if factual(s.text, pack) and not issued.intersection(s.handles)
    ]
    return _verdict("GR-CITATION", problems)


def factual(text: str, pack: LexiconPack) -> bool:
    """A sentence stating a product, regulatory or tax fact (a pack term), a number or a
    placeholder: what must carry a handle (GR-CITATION, and Step 23's citation coverage)."""
    return bool(
        pack.factual_terms.search(text) or _PLACEHOLDER.search(text) or _number_tokens(text, pack)
    )


def _verdict(rule_id: str, problems: list[str]) -> Verdict:
    return Verdict("grounding", rule_id, "fail" if problems else "pass", detail=tuple(problems))


class ClaimCheck(BaseModel):
    """verify-claims' response: does the evidence entail the claim."""

    model_config = ConfigDict(extra="forbid")

    verdict: Literal["entailed", "not_entailed"]


async def verify_claims(
    gateway: Gateway, text: str, ctx: OutputContext, settings: Settings
) -> Verdict:
    """Claim-level NLI (TDD §2.5): each cited sentence against what it cites. Every claim on
    gen-recommend (S3, side-queries); on gen-converse a share of turns, drawn from the turn id so
    a replay draws the same. The score is the share of claims entailed."""
    if not _nli_due(ctx, settings):
        return Verdict("grounding", "GR-NLI", "not_sampled")
    claims = [
        (s, premise)
        for s in sentences(normalise(text).text)
        if (premise := "\n".join(sources(s.handles, ctx.handles)))
    ]
    if not claims:
        return Verdict("grounding", "GR-NLI", "pass")
    results = await asyncio.gather(
        *(entailed(gateway, s.text, premise, ctx) for s, premise in claims),
        return_exceptions=True,
    )
    down = [r for r in results if isinstance(r, GatewayUnavailable)]
    if down:
        logger.warning("verify-claims unavailable (%s): holding the draft", down[0].reason)
        return Verdict(
            "grounding",
            "GR-NLI",
            "unavailable",
            detail=(f"GR-NLI: unavailable ({down[0].reason})",),
        )
    for r in results:
        if isinstance(r, BaseException):
            raise r
    problems = [
        f"GR-NLI sentence {s.n}: it is not supported by what it cites"
        for (s, _), entailed in zip(claims, results, strict=True)
        if entailed is not True
    ]
    score = 1 - len(problems) / len(claims)
    return Verdict("grounding", "GR-NLI", "fail" if problems else "pass", score, tuple(problems))


def _nli_due(ctx: OutputContext, settings: Settings) -> bool:
    if ctx.route is Route.GEN_RECOMMEND:
        return True
    draw = int.from_bytes(hashlib.sha256(ctx.turn_id.bytes).digest()[:8], "big") / 2**64
    return draw < settings.verify_sample_rate


async def entailed(gateway: Gateway, claim: str, premise: str, ctx: OutputContext) -> bool:
    plain = _PLACEHOLDER.sub(lambda m: f"[{m.group(1)}]", _CITATION.sub("", claim)).strip()
    result = await gateway.call(
        Route.VERIFY_CLAIMS,
        data_class=DataClass.SELF_HOSTED_RAW,
        messages=[
            {
                "role": "user",
                "content": f"<evidence>{html.escape(premise, quote=False)}</evidence>\n"
                f"<claim>{html.escape(plain, quote=False)}</claim>",
            }
        ],
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        fsm_state=ctx.fsm_state,
        response_format=ClaimCheck,
    )
    return result.parsed is not None and result.parsed.verdict == "entailed"


def disclosures(
    rendered: Rendered, numbers: Numbers | None, sets: Mapping[str, DisclosureSet]
) -> Verdict:
    """I4: every ranked option's disclosure part is in the released text, ends with the registry
    bodies verbatim, and hashes (body and set, recomputed here) to what the registry holds."""
    problems = []
    if hashlib.sha256(rendered.text.encode("utf-8")).hexdigest() != rendered.rendered_sha256:
        problems.append("rendered_sha256 does not match the text")
    parts = dict(rendered.parts)
    for uin in [o.uin for o in numbers.ranking.options] if numbers else []:
        shown, part = sets.get(uin), parts.get(f"disclosures:{uin}")
        if shown is None or not shown.items or part is None or part not in rendered.text:
            problems.append(f"{uin}: disclosure set missing")
            continue
        if not part.endswith("\n" + "\n".join(item.body for item in shown.items)):
            problems.append(f"{uin}: disclosure bodies differ from the registry")
        if any(content_sha256(item.body) != item.body_sha256 for item in shown.items):
            problems.append(f"{uin}: a body hash differs from the registry")
        if _set_sha256(shown) != shown.set_sha256 or rendered.disclosure_hashes.get(uin) != (
            shown.set_sha256
        ):
            problems.append(f"{uin}: set_sha256 differs from the registry")
    return Verdict(
        "release", "RC-DISCLOSURE", "block" if problems else "pass", None, tuple(problems)
    )


def _set_sha256(shown: DisclosureSet) -> str:
    """The contract's set_sha256, over the bodies as they will be shown."""
    return sha256_hex(
        {
            "registry_version": shown.registry_version,
            "uin": shown.uin,
            "channel": shown.channel,
            "language": shown.language,
            "items": [
                {"disclosure_id": i.disclosure_id, "body_sha256": content_sha256(i.body)}
                for i in shown.items
            ],
        }
    )


async def guard(gateway: Gateway, model_text: str | None, ctx: OutputContext) -> Verdict:
    """guard-input in output mode, on the model's text only: the rest is approved text."""
    if model_text is None:
        return Verdict("release", "RC-GUARD", "pass", detail=("no model text",))
    verdict = await call_guard(
        gateway,
        text=ctx.customer_text,
        response=model_text,
        session_id=ctx.session_id,
        turn_id=ctx.turn_id,
        fsm_state=ctx.fsm_state,
    )
    if verdict is None:
        return Verdict("release", "RC-GUARD", "block", detail=("guard-input unavailable",))
    if verdict.safety != "safe":
        return Verdict("release", "RC-GUARD", "block", detail=(f"guard: {verdict.safety}",))
    return Verdict("release", "RC-GUARD", "pass")


def leaks(
    text: str,
    ctx: OutputContext,
    sets: Mapping[str, DisclosureSet],
    *,
    model_text: str | None = None,
    instructions: str = "",
) -> Verdict:
    """Cross-session leaks: a redaction token, an id other than this session's, or PII that is
    neither this session's own nor in the approved registry text shown; and (Step 23) the model's
    own text repeating its instructions (L0 or an L1) word for word. Kinds only, never values."""
    kinds = []
    if model_text and instructions and _echoes(model_text, instructions):
        kinds.append("prompt_echo")
    if _TOKEN.search(text):
        kinds.append("redaction_token")
    own_ids = {str(ctx.session_id), str(ctx.subject_ref)}
    if any(u.lower() not in own_ids for u in _UUID.findall(text)):
        kinds.append("foreign_id")
    bodies = [item.body for shown in sets.values() for item in shown.items]
    approved = {
        body[start:end]
        for body in (*bodies, *ctx.approved_text)
        for _, start, end in redact.entities(body)
    }
    kinds += [
        f"pii:{kind}"
        for kind, start, end in redact.entities(text)
        if text[start:end] not in ctx.own_pii | approved
    ]
    kinds = list(dict.fromkeys(kinds))
    return Verdict("release", "RC-LEAK", "block" if kinds else "pass", None, tuple(kinds))


_ECHO_WORDS = 8  # a window this long, verbatim from the instructions, is an echo, not a paraphrase


def _windows(text: str) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", normalise(text).text.casefold())
    return {tuple(words[i : i + _ECHO_WORDS]) for i in range(len(words) - _ECHO_WORDS + 1)}


def _echoes(model_text: str, instructions: str) -> bool:
    return not _windows(model_text).isdisjoint(_windows(instructions))


def instructions(bundle: PromptBundle) -> str:
    """Every instruction layer a generation route can see: L0 and each L1."""
    return "\n".join([bundle.l0, *bundle.l1.values()])


def dummy(text: str, env: str) -> Verdict:
    """DUMMY seed text: blocked in pilot and prod, counted in dev and test (the score)."""
    count = text.count("DUMMY")
    action = "pass" if not count else ("block" if env in ("pilot", "prod") else "count")
    return Verdict("release", "RC-DUMMY", action, float(count))


async def release(
    conn: Conn,
    keys: KeyService,
    gateway: Gateway,
    ctx: OutputContext,
    *,
    pack: LexiconPack,
    settings: Settings,
    bundle: PromptBundle,
    draft: str | None,  # the generation route's text; None when the route was down
    # envelope.build(..., corrections=errors), then the same route; None when no regeneration
    # could be made (route down, or the envelope refused, e.g. PII_IN_ENVELOPE)
    regenerate: Callable[[list[str]], Awaitable[str | None]],
    render: Callable[[str | None], Rendered],  # the composer; None: the card or the template
    numbers: Numbers | None = None,
    disclosure_sets: Mapping[str, DisclosureSet] | None = None,
) -> Released:
    """Rails 6-7 on the draft, one regeneration on failure, the fallback after a second failure
    or when the verifier cannot run; then rail 8 on what was rendered. CompositionError from
    render propagates (an input defect, not a model one), after rails 6-7 are audited."""
    sets = disclosure_sets or {}
    audited: list[Verdict] = []
    chosen: str | None = None
    passed_on = 0  # the attempt whose text passed
    text = draft
    for attempt in (1, 2):
        if text is None:
            break
        verdicts = lexicon(text, ctx, pack) + grounding(text, ctx, pack, numbers)
        if any(v.action == "fail" for v in verdicts):
            verdicts.append(Verdict("grounding", "GR-NLI", "skipped"))
        else:
            verdicts.append(await verify_claims(gateway, text, ctx, settings))
        failed = [v for v in verdicts if v.action in ("fail", "unavailable")]
        final = attempt == 2 or any(v.action == "unavailable" for v in failed)
        consequence = "fallback" if final else "regenerate"
        audited += [
            replace(v, action=consequence) if v.action in ("fail", "unavailable") else v
            for v in verdicts
        ]
        if not failed:
            chosen, passed_on = text, attempt
            break
        logger.info(
            "output rails attempt %d failed: %s -> %s",
            attempt,
            ", ".join(v.rule_id for v in failed),
            consequence,
        )
        if final:
            break
        text = await regenerate([line for v in failed for line in v.detail])
    _emit(conn, keys, ctx, pack, audited)

    rendered = render(chosen)
    checks = [
        disclosures(rendered, numbers, sets),
        await guard(gateway, chosen, ctx),
        leaks(rendered.text, ctx, sets, model_text=chosen, instructions=instructions(bundle)),
        dummy(rendered.text, settings.env),
    ]
    _emit(conn, keys, ctx, pack, checks)
    verdicts_by_check = {f"{v.rail}:{v.rule_id}": v.action for v in audited + checks}
    if blocked := [v.rule_id for v in checks if v.action == "block"]:
        logger.warning("release blocked by %s: template and advisor offer", ", ".join(blocked))
        scripts = bundle.templates[ctx.locale].scripts
        text_out = f"{scripts.release_blocked}\n{scripts.advisor_offer}"
        return Released("blocked", text_out, None, verdicts_by_check)
    kind: Literal["narrative", "regenerated", "fallback"] = (
        "fallback" if chosen is None else ("narrative" if passed_on == 1 else "regenerated")
    )
    logger.info("output rails passed: %s, %d verdicts", kind, len(audited) + len(checks))
    return Released(kind, rendered.text, rendered, verdicts_by_check)


def _emit(
    conn: Conn, keys: KeyService, ctx: OutputContext, pack: LexiconPack, verdicts: Sequence[Verdict]
) -> None:
    for v in verdicts:
        logger.debug("verdict %s %s: %s", v.rail, v.rule_id, v.action)
        audit_chain.append(
            conn,
            keys,
            session_id=ctx.session_id,
            event_type=EventType.GUARD_VERDICT,
            fsm_state=ctx.fsm_state,
            pins=ctx.pins,
            header=GuardVerdictHeader(
                rail=v.rail,
                rule_id=v.rule_id,
                score=v.score,
                action=v.action,
                pack_version=pack.version if v.rail == "lexicon" else None,
            ),
            payload={"detail": list(v.detail)},
            key_ref=ctx.key_ref,
        )
