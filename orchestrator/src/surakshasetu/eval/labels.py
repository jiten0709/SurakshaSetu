"""The labelled slot and intent sets (content/golden/slots, content/golden/intents; Step 23) and
their runner: each utterance goes through the production analysis (analysis.pipeline.analyse, the
same rails and nlu-extract a turn uses) and the state's own slot reading, so the scores measure the
system, against the stub in `make eval` and the provisioned routes in `make eval-live`.

A missing or empty directory, malformed YAML, an unknown slot, state or intent, or an id used twice
fails loudly, naming the file. Logs carry ids and counts only, never an utterance or a value.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from surakshasetu.analysis.models import Intent
from surakshasetu.analysis.nlu import PendingSlotSpec
from surakshasetu.analysis.pipeline import analyse
from surakshasetu.config import Settings
from surakshasetu.domain.client import DomainClient
from surakshasetu.fsm.states import FsmState
from surakshasetu.gateway import Gateway
from surakshasetu.graph.handlers import safety
from surakshasetu.graph.states import KNOWN_SLOTS, quote_only, s1, s2
from surakshasetu.uuid7 import uuid7

logger = logging.getLogger(__name__)

GOLDEN = Path(__file__).resolve().parents[4] / "content" / "golden"
SLOTS = GOLDEN / "slots"
INTENTS = GOLDEN / "intents"
Language = Literal["en", "hi", "hi-Latn"]
SlotState = Literal["S1", "QUOTE_ONLY", "S2"]
KNOWN = frozenset(s1.KINDS) | frozenset(s2.NEEDS)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SlotItem(_Strict):
    id: str = Field(pattern=r"^sl-[a-z0-9-]{3,60}$")
    language: Language
    state: SlotState
    pending_slot: str  # the question just asked
    text: str = Field(min_length=1)
    gold: dict[str, Any]  # slot -> the value as stored; {} when the turn answers nothing

    @model_validator(mode="after")
    def _known(self) -> Self:
        kinds = s2.NEEDS if self.state == "S2" else s1.KINDS
        if self.pending_slot not in kinds:
            raise ValueError(f"{self.id}: {self.pending_slot} is not a {self.state} slot")
        if unknown := sorted(set(self.gold) - KNOWN):
            raise ValueError(f"{self.id}: unknown slots {unknown}")
        return self


class IntentItem(_Strict):
    id: str = Field(pattern=r"^in-[a-z0-9-]{3,60}$")
    language: Language
    state: Literal["S0", "S1", "QUOTE_ONLY", "S2", "S3"]
    text: str = Field(min_length=1)
    intents: list[Intent] = Field(min_length=1)


class SlotSet(_Strict):
    description: str
    is_dummy: bool
    items: list[SlotItem] = Field(min_length=1)


class IntentSet(_Strict):
    description: str
    is_dummy: bool
    items: list[IntentItem] = Field(min_length=1)


def load_slots(root: Path | None = None) -> list[SlotItem]:
    return [i for s in _files(root or SLOTS, SlotSet) for i in s.items]  # type: ignore[attr-defined]


def load_intents(root: Path | None = None) -> list[IntentItem]:
    return [i for s in _files(root or INTENTS, IntentSet) for i in s.items]  # type: ignore[attr-defined]


def _files(root: Path, model: type[BaseModel]) -> list[BaseModel]:
    paths = sorted(p for p in root.glob("*.yaml") if not p.name.startswith("."))
    if not paths:
        raise FileNotFoundError(f"no label files in {root}")
    sets = []
    for path in paths:
        try:
            sets.append(model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8"))))
        except (ValueError, yaml.YAMLError) as exc:
            raise ValueError(f"{path.name}: {exc}") from exc
    ids = [i.id for s in sets for i in s.items]  # type: ignore[attr-defined]
    if len(ids) != len(set(ids)):
        raise ValueError(f"{root.name}: an id is used twice")
    return sets


# --- the runner -----------------------------------------------------------------------------------
@dataclass(frozen=True)
class SlotResult:
    item: SlotItem
    predicted: dict[str, Any]


@dataclass(frozen=True)
class IntentResult:
    item: IntentItem
    predicted: set[str]


async def asked_slots(domain: DomainClient) -> dict[str, frozenset[str]]:
    """What each state may fill: the pinned rules' attributes (S1; Quote-Only's quote slots and
    stated values) and required slots (S2), as the Screens read them."""
    rules = (await domain.get_versions()).rules_version
    attributes = {a.attribute for a in await domain.get_required_attributes(rules)}
    needs = {r.slot for r in await domain.get_required_slots(rules)}
    quote = (attributes & set(quote_only.QUOTE_SLOTS)) | set(quote_only.STATED)
    return {"S1": frozenset(attributes), "QUOTE_ONLY": frozenset(quote), "S2": frozenset(needs)}


async def run_slots(
    items: Sequence[SlotItem],
    gateway: Gateway,
    domain: DomainClient,
    settings: Settings,
    asked: dict[str, frozenset[str]],
) -> list[SlotResult]:
    results = []
    for item in items:
        state = FsmState(item.state)
        found = await analyse(
            gateway,
            item.text,
            session_id=uuid7(),
            turn_id=uuid7(),
            fsm_state=state.value,
            pending=PendingSlotSpec(item.pending_slot, KNOWN_SLOTS[state]),
            settings=settings,
        )
        results.append(SlotResult(item, await _read(item, found.result, domain, settings, asked)))
    logger.info("slot labels: %d utterances read", len(results))
    return results


async def _read(
    item: SlotItem,
    result: Any,
    domain: DomainClient,
    settings: Settings,
    asked: dict[str, frozenset[str]],
) -> dict[str, Any]:
    """Screen.candidates, then Screen.answer (graph/states/s1.py), without a session: each nlu
    candidate the state may take (aliased, at or above the read-back floor), then, when nlu-extract
    offered nothing for the open question, the deterministic reading of the whole message. A
    blocked turn keeps nothing. Keep in step with Screen.take."""
    s2_state = item.state == "S2"
    kinds = s2.NEEDS if s2_state else s1.KINDS
    aliases = s2.ALIASES if s2_state else s1.ALIASES
    predicted: dict[str, Any] = {}
    offered: set[str] = set()
    if result.blocked:
        return predicted
    for candidate in result.analysis.slots if result.analysis else []:
        slot = aliases.get(candidate.slot, candidate.slot)
        if slot not in kinds:
            continue
        offered.add(slot)
        if slot in asked[item.state] and candidate.confidence >= settings.confidence_readback_floor:
            value = await _understand(
                item.state, slot, candidate.value, candidate.evidence_span, domain
            )
            if value is not _NONE:
                predicted[slot] = value
    pending = item.pending_slot
    if pending not in offered and pending not in predicted:
        value = await _understand(item.state, pending, result.stored_raw, result.stored_raw, domain)
        if value is not _NONE:
            predicted[pending] = value
    return predicted


_NONE = object()  # nothing usable (a declined value is None, and is an answer)


async def _understand(state: str, slot: str, raw: Any, evidence: str, domain: DomainClient) -> Any:
    if state == "S2":
        answer: Any = s2.understand_value(slot, raw, evidence)
    else:  # s1.understand only reaches turn.domain (pincode master, occupation search)
        answer = await s1.understand(SimpleNamespace(domain=domain), slot, raw, evidence)
    if answer is None or isinstance(answer, list | s2.Period):
        return _NONE  # unusable, several occupations to choose from, or a period still to ask
    return answer.value


async def run_intents(
    items: Sequence[IntentItem], gateway: Gateway, settings: Settings
) -> list[IntentResult]:
    """The intents a turn ends up with: nlu-extract's after the block filter (a blocked turn keeps
    only a withdrawal), and SAFETY when the safety rail blocked it (it fails closed on its lexicon
    when the guard is down), as graph/handlers/safety.signal reads them."""
    results = []
    for item in items:
        found = await analyse(
            gateway,
            item.text,
            session_id=uuid7(),
            turn_id=uuid7(),
            fsm_state=item.state,
            pending=PendingSlotSpec(None, KNOWN_SLOTS.get(FsmState(item.state), ())),
            settings=settings,
        )
        intents = (
            {i.value for i in found.result.analysis.intents} if found.result.analysis else set()
        )
        if safety.signal(found.result):
            intents.add(Intent.SAFETY.value)
        results.append(IntentResult(item, intents))
    logger.info("intent labels: %d turns read", len(results))
    return results
