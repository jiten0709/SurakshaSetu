"""Evidence selection and the sufficiency gate (TDD §2.4 steps 4-5, §1.5's evidence budget). Pure
and deterministic: the service does the I/O, this decides what is kept and whether it suffices.

- select: each quota domain's best first, then the best overall up to `keep`; then the lowest score
  goes first until the evidence fits the token budget, never below a quota;
- parents: a kept clause brings the longest proper section_path prefix of its document, while the
  budget allows;
- insufficient: abstain when no kept chunk reaches its snapshot's threshold, or a quota domain has
  fewer passing chunks than its quota.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from surakshasetu.kb.chunker import count_tokens
from surakshasetu.kb.payload import Collection, KbPayload

NO_SUFFICIENT_EVIDENCE = "NO_SUFFICIENT_EVIDENCE"
QUOTA_UNMET = "QUOTA_UNMET"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Stamp(_Strict):
    model_id: str
    model_sha: str


class SnapshotThresholds(_Strict):
    rerank: float = Field(ge=0, le=1)  # on the reranker's 0-1 score
    bm25: float = Field(ge=0)  # on Qdrant's BM25 score, when the reranker is down


class Thresholds(_Strict):
    """content/kb/thresholds.yaml, written by `make calibrate-retrieval`. Keyed by snapshot id, so a
    session keeps the thresholds of the snapshot it pinned (I7), and valid only for the reranker
    they were calibrated with."""

    degraded_factor: float = Field(ge=1)  # BM25-only retrieval is held to a stricter threshold
    reranker: Stamp
    snapshots: dict[str, SnapshotThresholds]


@dataclass(frozen=True)
class Scored:
    payload: KbPayload
    score: float | None  # None only for a parent fetched from outside the candidate pool
    parent: bool = False


def rank_key(item: Scored) -> tuple[float, str]:
    return (-(item.score if item.score is not None else float("-inf")), item.payload.chunk_id)


def select(
    ranked: Iterable[Scored], quotas: Mapping[Collection, int], *, keep: int, budget: int
) -> list[Scored]:
    ranked = sorted(ranked, key=rank_key)
    chosen: dict[str, Scored] = {}
    for domain, n in quotas.items():
        for item in [s for s in ranked if s.payload.domain == domain][:n]:
            chosen[item.payload.chunk_id] = item
    for item in ranked:
        if len(chosen) >= keep:
            break
        chosen.setdefault(item.payload.chunk_id, item)
    kept = sorted(chosen.values(), key=rank_key)
    while tokens(kept) > budget:
        spare = next((s for s in reversed(kept) if _above_quota(kept, s, quotas)), None)
        if spare is None:
            break
        kept.remove(spare)
    return kept


def parent_of(child: KbPayload, documents: Iterable[KbPayload]) -> KbPayload | None:
    """The chunk of the same document and snapshot whose section_path is the longest proper prefix
    of the child's."""
    best: KbPayload | None = None
    for other in documents:
        depth = len(other.section_path)
        if (
            other.doc_id == child.doc_id
            and other.snapshot_id == child.snapshot_id
            and depth < len(child.section_path)
            and child.section_path[:depth] == other.section_path
            and (best is None or depth > len(best.section_path))
        ):
            best = other
    return best


def add_parents(
    kept: list[Scored],
    documents: list[KbPayload],
    scores: Mapping[str, float],
    *,
    budget: int,
) -> list[Scored]:
    """kept, then the parents of its clauses in kept order, each while the budget allows."""
    evidence = list(kept)
    present = {s.payload.chunk_id for s in kept}
    for item in kept:
        parent = parent_of(item.payload, documents)
        if parent is None or parent.chunk_id in present:
            continue
        candidate = Scored(parent, scores.get(parent.chunk_id), parent=True)
        if tokens([*evidence, candidate]) <= budget:
            evidence.append(candidate)
            present.add(parent.chunk_id)
    return evidence


def insufficient(
    kept: Iterable[Scored],
    quotas: Mapping[Collection, int],
    threshold: Callable[[KbPayload], float],
) -> str | None:
    passing = [
        s for s in kept if not s.parent and s.score is not None and s.score >= threshold(s.payload)
    ]
    if not passing:
        return NO_SUFFICIENT_EVIDENCE
    for domain, n in quotas.items():
        if sum(s.payload.domain == domain for s in passing) < n:
            return QUOTA_UNMET
    return None


def tokens(items: Iterable[Scored]) -> int:
    """The proxy token count the chunker sizes chunks with (Step 11: about 16% under the embedder's
    own tokenizer for English)."""
    return sum(count_tokens(s.payload.text) for s in items)


def _above_quota(kept: list[Scored], item: Scored, quotas: Mapping[Collection, int]) -> bool:
    domain = item.payload.domain
    return sum(s.payload.domain == domain for s in kept) > quotas.get(domain, 0)
