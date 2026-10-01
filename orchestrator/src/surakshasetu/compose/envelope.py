"""The context envelope (TDD §1.5, §3.4): the exact, provider-neutral messages a generation route
receives, ordered static to dynamic so the L0 prefix stays cacheable:

    system  L0 constitution ({insurer}), then the L1 state instructions ({next_slot})
    user    <session_facts>, <summary>, <recent_turns>, ENGINE_RESULT <engine> blocks,
            EVIDENCE <evidence> blocks, VALIDATOR_ERRORS <error> lines (a regeneration only,
            Step 14), then the turn's <user_input>

It is built only from redacted text and SessionFacts, which hold no numbers and no identifiers
(decided 2026-10-01). Every tagged text is escaped, so customer or corpus text cannot close a tag.
Budgets come from the pinned bundle and are counted in the proxy tokens of Steps 10-12:
- L0, L1, the facts and the summary are never trimmed: over budget is an EnvelopeError;
- recent turns lose the oldest whole turn first;
- evidence loses parents, then the lowest-scored chunk, never below the route's quota;
- the user turn is a hard cap: a longer turn is declined (USER_TURN_TOO_LONG).

The finished envelope is scanned by the Step 10 redactor. Any hit blocks the call, and the caller
answers from a template (fail closed). Only a clean envelope carries the RedactionAttestation the
gateway requires for the REDACTED routes. envelope_sha256 = SHA-256(JCS(messages)) goes on the
MODEL_CALL header and the messages into its encrypted payload, for replay. Nothing here logs text.
"""

import html
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from surakshasetu.audit.events import ModelCallHeader
from surakshasetu.compose.bundle import L1_ROUTES, L1Name, PromptBundle
from surakshasetu.compose.citations import EngineFact, TurnHandles, issue
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.domain.models import Goal, IncomeType
from surakshasetu.gateway import DataClass, GatewayResult, RedactionAttestation, Route
from surakshasetu.kb.chunker import count_tokens
from surakshasetu.kb.payload import Collection
from surakshasetu.rails.redact import redact
from surakshasetu.retrieval.service import EvidenceChunk, RetrievalResult

logger = logging.getLogger(__name__)


class EnvelopeError(Exception):
    """No envelope, so no model call: the caller takes its template path (ask_to_shorten for
    USER_TURN_TOO_LONG). reason: USER_TURN_TOO_LONG, OVER_BUDGET:<section>, MISSING_NEXT_SLOT,
    LOCALE_UNKNOWN or PII_IN_ENVELOPE."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SessionFacts(BaseModel):
    """What a REDACTED route may know about the customer (decided 2026-10-01): which slots are
    answered, and the needs that carry no number and identify no one. The model reaches engine
    values only through placeholders."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: Literal["en", "hi", "hi-Latn"]
    answered_slots: list[str] = []
    goals: list[Goal] = []
    income_type: IncomeType | None = None
    dependant_relations: list[Literal["spouse", "child", "parent", "other"]] = []
    liability_kinds: list[
        Literal["home", "vehicle", "education", "personal", "business", "other"]
    ] = []


@dataclass(frozen=True)
class Turn:
    customer: str  # the redacted text (rails.redact), never stored_raw
    assistant: str  # the released text


@dataclass(frozen=True)
class Envelope:
    route: Route
    messages: list[dict[str, str]]
    sha256: str  # SHA-256(JCS(messages))
    attestation: RedactionAttestation  # issued only after a clean scan of exactly these messages
    handles: TurnHandles  # the handles this envelope carries: only these may be cited
    tokens: dict[str, int]  # per section, after trimming
    data_class: DataClass = DataClass.REDACTED


def build(
    bundle: PromptBundle,
    *,
    l1: L1Name,
    locale: str,
    user_text: str,
    facts: SessionFacts,
    summary: str | None = None,
    recent: Sequence[Turn] = (),
    retrieval: RetrievalResult | None = None,
    engine: Sequence[EngineFact] = (),
    next_slot: str | None = None,
    corrections: Sequence[str] = (),  # the output rails' error list, for the one regeneration
) -> Envelope:
    route = L1_ROUTES[l1]
    budgets = bundle.manifest.budgets
    templates = bundle.templates.get(locale)
    if templates is None:
        raise _error(route, "LOCALE_UNKNOWN")
    if count_tokens(user_text) > budgets.user_turn:
        raise _error(route, "USER_TURN_TOO_LONG")
    state = bundle.l1[l1]
    if "{next_slot}" in state:
        slot = templates.slots.get(next_slot or "")
        if slot is None:
            raise _error(route, "MISSING_NEXT_SLOT")
        state = state.replace("{next_slot}", f"{slot.question} {slot.reason}")

    turns = list(recent)
    while turns and count_tokens(_turns(turns)) > budgets.recent_turns:
        turns.pop(0)
    issued = issue(retrieval.evidence if retrieval else [], engine)
    chunks = list(issued.evidence.values())
    quotas = retrieval.quotas if retrieval else {}
    while count_tokens(_evidence(issued.engine, chunks)) > budgets.evidence:
        spare = next((c for c in reversed(chunks) if _spare(chunks, c, quotas)), None)
        if spare is None:
            break  # never below a quota, as retrieval's own selection
        chunks.remove(spare)

    sections = {
        "constitution": bundle.l0.replace("{insurer}", bundle.manifest.insurer),
        "state": state,
        "facts": _tag(
            "session_facts",
            json.dumps(facts.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
        ),
        "summary": _tag("summary", summary) if summary else "",
        "recent_turns": _turns(turns),
        "evidence": _evidence(issued.engine, chunks),
        "corrections": _corrections(corrections),
        "user_turn": _tag("user_input", user_text),
    }
    tokens = {name: count_tokens(text) for name, text in sections.items()}
    for name in ("constitution", "state", "facts", "summary"):
        if tokens[name] > getattr(budgets, name):
            raise _error(route, f"OVER_BUDGET:{name}")
    system = sections["constitution"].rstrip("\n") + "\n\n" + sections["state"]
    dynamic = ("facts", "summary", "recent_turns", "evidence", "corrections", "user_turn")
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(sections[s] for s in dynamic if sections[s])},
    ]
    if any(redact(m["content"]).redacted != m["content"] for m in messages):
        raise _error(route, "PII_IN_ENVELOPE")
    sha256 = sha256_hex(messages)
    logger.debug(
        "envelope %s: %s tokens; dropped %d turns, %d evidence chunks",
        route,
        tokens,
        len(recent) - len(turns),
        len(issued.evidence) - len(chunks),
    )
    return Envelope(
        route=route,
        messages=messages,
        sha256=sha256,
        attestation=RedactionAttestation(sha256),
        handles=TurnHandles({c.handle: c for c in chunks}, issued.engine),
        tokens=tokens,
    )


def model_call_event(
    envelope: Envelope, result: GatewayResult[Any]
) -> tuple[ModelCallHeader, dict[str, Any]]:
    """The MODEL_CALL header and payload; the caller appends them in the turn's transaction."""
    header = ModelCallHeader(
        route=envelope.route.value,
        served_model=result.served_model,
        envelope_sha256=envelope.sha256,
        tokens_in=result.tokens_in,
        tokens_out=result.tokens_out,
        latency_ms=round(result.latency_ms),
        fallback_hops=result.fallback_hops,
    )
    return header, {"route": envelope.route.value, "messages": envelope.messages}


def summary_due(bundle: PromptBundle, turn_seq: int) -> bool:
    """Whether this turn regenerates the rolling summary (through the summarise route)."""
    return turn_seq > 0 and turn_seq % bundle.manifest.summary_every_turns == 0


def _tag(name: str, text: str) -> str:
    return f"<{name}>{html.escape(text, quote=False)}</{name}>"


def _attr(value: object) -> str:
    return html.escape(str(value), quote=True)


def _turns(turns: Sequence[Turn]) -> str:
    if not turns:
        return ""
    body = "\n".join(
        f"<turn>{_tag('user_input', t.customer)}\n{_tag('assistant', t.assistant)}</turn>"
        for t in turns
    )
    return f"<recent_turns>\n{body}\n</recent_turns>"


def _evidence(engine: Mapping[str, EngineFact], chunks: Sequence[EvidenceChunk]) -> str:
    lines: list[str] = []
    if engine:
        lines.append("ENGINE_RESULT:")
        lines += [
            f'<engine id="{h}" rule="{_attr(f.rule)}">'
            f"{html.escape(json.dumps(f.content, ensure_ascii=False, sort_keys=True), quote=False)}"
            "</engine>"
            for h, f in engine.items()
        ]
    if chunks:
        lines.append("EVIDENCE:")
        lines += [
            f'<evidence id="{c.handle}" label="{_attr(c.citation_label)}"'
            f' precedence="{c.precedence}" parent="{str(c.parent).lower()}">'
            f"{html.escape(c.text, quote=False)}</evidence>"
            for c in chunks
        ]
    return "\n".join(lines)


def _corrections(errors: Sequence[str]) -> str:
    if not errors:
        return ""
    return "\n".join(["VALIDATOR_ERRORS:", *(_tag("error", e) for e in errors)])


def _spare(
    chunks: Sequence[EvidenceChunk], chunk: EvidenceChunk, quotas: Mapping[Collection, int]
) -> bool:
    """A parent can always go; a kept chunk only while its domain stays above its quota."""
    kept = sum(c.domain == chunk.domain and not c.parent for c in chunks)
    return chunk.parent or kept > quotas.get(chunk.domain, 0)


def _error(route: Route, reason: str) -> EnvelopeError:
    logger.warning("envelope for %s refused: %s", route, reason)
    return EnvelopeError(reason)
