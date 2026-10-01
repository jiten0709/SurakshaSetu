"""Turn-local citation handles (TDD §2.5, §3.8). Evidence keeps the E handles retrieval issued;
engine results get R handles here. The model cites [E3] or [R2]; a handle not issued this turn is
refused, and valid ones render as [Source: <label>], with a source list (title, section, version,
effective date and a deep link) for the evidence cited.

Engine facts enter the prompt, and the model never sees a premium or a cover amount, so a fact
carrying a money field is refused here.
"""

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from surakshasetu.compose.bundle import Recommendation
from surakshasetu.compose.placeholders import format_date
from surakshasetu.retrieval.service import EvidenceChunk

logger = logging.getLogger(__name__)

# [E3], [R2], or a list such as [E1, E4].
_CITATION = re.compile(r"\[([ER]\d+(?:\s*,\s*[ER]\d+)*)\]")
_MONEY_KEY = re.compile(r"_inr$|premium|quote", re.IGNORECASE)


class CitationError(Exception):
    """The text cites a handle that was not issued this turn."""

    def __init__(self, handle: str) -> None:
        super().__init__(handle)
        self.handle = handle


@dataclass(frozen=True)
class EngineFact:
    """One engine result the model may cite: the rule or reason code it rests on, the label a
    customer sees, and JSON-ready content without money."""

    rule: str
    label: str
    content: Mapping[str, Any]


@dataclass(frozen=True)
class TurnHandles:
    evidence: Mapping[str, EvidenceChunk]  # E# -> chunk
    engine: Mapping[str, EngineFact]  # R# -> fact

    def evidence_map(self, handles: Sequence[str] | None = None) -> dict[str, str]:
        """E# -> chunk_id and R# -> rule (TDD RecommendationPayload.evidence_map)."""
        wanted = list(self.evidence) + list(self.engine) if handles is None else handles
        return {
            h: self.evidence[h].chunk_id if h in self.evidence else self.engine[h].rule
            for h in wanted
        }


@dataclass(frozen=True)
class Source:
    title: str
    section: str
    version: str
    effective_from: date
    uri: str


@dataclass(frozen=True)
class Cited:
    text: str
    handles: list[str]  # cited, in first-cited order
    sources: list[Source]  # one per cited chunk, in first-cited order


def issue(evidence: Sequence[EvidenceChunk], facts: Sequence[EngineFact]) -> TurnHandles:
    for fact in facts:
        if _money_keys(fact.content):
            raise ValueError(f"engine fact {fact.rule} carries a money field")
    return TurnHandles(
        evidence={chunk.handle: chunk for chunk in evidence},
        engine={f"R{i}": fact for i, fact in enumerate(facts, start=1)},
    )


def render(text: str, handles: TurnHandles) -> Cited:
    cited = list(
        dict.fromkeys(h.strip() for group in _CITATION.findall(text) for h in group.split(","))
    )
    for handle in cited:
        if handle not in handles.evidence and handle not in handles.engine:
            logger.warning("citation refused: handle not issued this turn")
            raise CitationError(handle)

    def label(handle: str) -> str:
        if handle in handles.evidence:
            return handles.evidence[handle].citation_label
        return handles.engine[handle].label

    rendered = _CITATION.sub(
        lambda m: " ".join(f"[Source: {label(h.strip())}]" for h in m.group(1).split(",")), text
    )
    chunks = {handles.evidence[h].chunk_id: handles.evidence[h] for h in cited if h[0] == "E"}
    sources = [
        Source(c.doc_title, c.section_path[-1], c.version, c.effective_from, c.source_uri)
        for c in chunks.values()
    ]
    logger.debug("citations: %d handles cited, %d sources", len(cited), len(sources))
    return Cited(rendered, cited, sources)


def source_list(sources: Sequence[Source], templates: Recommendation) -> str:
    lines = [
        templates.source_line.format(
            title=s.title,
            section=s.section,
            version=s.version,
            effective_from=format_date(s.effective_from),
            uri=s.uri,
        )
        for s in sources
    ]
    return "\n".join([templates.sources_heading, *lines])


def _money_keys(node: Any) -> bool:
    if isinstance(node, Mapping):
        return any(_MONEY_KEY.search(str(k)) or _money_keys(v) for k, v in node.items())
    if isinstance(node, list | tuple):
        return any(_money_keys(v) for v in node)
    return False
