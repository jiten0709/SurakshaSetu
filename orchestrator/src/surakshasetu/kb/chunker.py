"""Structure-aware chunking (TDD §2.2): one chunk per section of a document's heading tree, with a
deterministic breadcrumb and no generated context. Pure: the ingest pipeline turns Docling's output
into Blocks first.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from surakshasetu.rails.normalise import TOKEN_RE

# Chunk sizes in tokens per collection (TDD §2.2). Max is hard; min is a target: small children
# merge into their parent when the whole parent fits.
SIZE_BOUNDS: dict[str, tuple[int, int]] = {
    "regulatory": (150, 450),
    "product": (250, 600),
    "tax": (150, 400),
}

# A proviso, explanation or illustration belongs to the clause before it: it never becomes a
# section of its own, and a split never starts with one.
_ATTACHED = re.compile(r"(provided|proviso|explanation|illustration)\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+[\dA-Za-z.()]*")


@dataclass(frozen=True)
class Block:
    level: int | None  # 1-6 for a heading (1 is the document title), None for a paragraph
    text: str


@dataclass(frozen=True)
class RawChunk:
    section_id: str
    section_path: tuple[str, ...]
    text: str


class ChunkingError(ValueError):
    """The document cannot be chunked within the size bounds; the message names the section."""


@dataclass
class _Section:
    heading: str | None
    level: int
    path: tuple[str, ...]
    paragraphs: list[str] = field(default_factory=list)
    children: list["_Section"] = field(default_factory=list)


# ponytail: word-and-punctuation count, a proxy for the embedding model's subword tokens.
def count_tokens(text: str) -> int:
    return len(TOKEN_RE.findall(text))


def section_id(heading: str) -> str:
    """The heading's leading number ("5.3", "123(1)"), else a slug of the heading."""
    if m := _NUMBER.match(heading):
        return m.group().rstrip(".")
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


def chunk_document(
    blocks: Sequence[Block], *, root: Sequence[str], bounds: tuple[int, int]
) -> list[RawChunk]:
    """root is the breadcrumb before the headings (e.g. product and document); it replaces the
    document title."""
    return _emit(_tree(blocks, tuple(root)), *bounds)


def _tree(blocks: Sequence[Block], root: tuple[str, ...]) -> _Section:
    top = _Section(None, 1, root)
    stack = [top]
    for block in blocks:
        if block.level == 1:
            continue
        if block.level is None or _ATTACHED.match(block.text):
            stack[-1].paragraphs.append(block.text)
            continue
        while stack[-1].level >= block.level:
            stack.pop()
        section = _Section(block.text, block.level, (*stack[-1].path, block.text))
        stack[-1].children.append(section)
        stack.append(section)
    return top


def _emit(section: _Section, low: int, high: int) -> list[RawChunk]:
    sid = section_id(section.heading) if section.heading else "preamble"
    if (
        section.heading
        and section.children
        and count_tokens(_text(section)) <= high
        and any(count_tokens(_text(c)) < low for c in section.children)
    ):
        return [RawChunk(sid, section.path, _text(section))]
    chunks = [RawChunk(sid, section.path, part) for part in _split(section, high)]
    for child in section.children:
        chunks += _emit(child, low, high)
    return chunks


def _text(section: _Section) -> str:
    """The section's own paragraphs, then each child's heading and text, depth first."""
    parts = list(section.paragraphs)
    for child in section.children:
        parts += [child.heading or "", _text(child)]
    return "\n\n".join(p for p in parts if p)


def _split(section: _Section, high: int) -> list[str]:
    """Pack the section's own paragraphs into parts of at most high tokens, keeping each clause
    with the provisos and explanations that follow it."""
    units: list[list[str]] = []
    for paragraph in section.paragraphs:
        if units and _ATTACHED.match(paragraph):
            units[-1].append(paragraph)
        else:
            units.append([paragraph])
    parts: list[list[str]] = []
    for unit in units:
        if count_tokens("\n\n".join(unit)) > high:
            raise ChunkingError(f"{' › '.join(section.path)}: a clause is over {high} tokens")
        if parts and count_tokens("\n\n".join([*parts[-1], *unit])) <= high:
            parts[-1] += unit
        else:
            parts.append(list(unit))
    return ["\n\n".join(part) for part in parts]
