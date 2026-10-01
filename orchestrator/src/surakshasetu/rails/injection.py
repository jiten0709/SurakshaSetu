"""Rail 3 (TDD §4.2): prompt-injection detection. Heuristics run locally and always apply; the
guard-input route adds a model score on top when it's reachable. `GuardVerdict` is the response
schema for the one guard-input call `analysis.pipeline` makes per turn; rails/safety.py reuses it
so both rails read the same call.
"""

import logging
import re
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from surakshasetu.gateway import DataClass, Gateway, GatewayUnavailable, Route

logger = logging.getLogger(__name__)


class GuardVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    injection_score: float = Field(ge=0, le=1)
    safety: str


@dataclass(frozen=True)
class InjectionVerdict:
    hit: bool
    rule_id: str  # a heuristic pattern name, "guard-input", or "none"
    score: float | None  # the guard's injection_score; None when the guard route was unavailable


# name -> pattern. EN/HI/Hinglish instruction overrides, role-play, fake system text, markup
# smuggling and long base64 runs.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("override_en", re.compile(r"ignore (all |the )?(previous|above|prior) instructions", re.I)),
    ("override_en_2", re.compile(r"disregard (your|all|the) (instructions|rules|prompt)", re.I)),
    ("override_en_3", re.compile(r"forget (your|all|the) (instructions|training|rules)", re.I)),
    ("roleplay_en", re.compile(r"\byou are now\b|\bpretend (you are|to be)\b|\back as\b", re.I)),
    ("dev_mode", re.compile(r"\bdeveloper mode\b|\bjailbreak\b|\bDAN mode\b", re.I)),
    ("fake_system_en", re.compile(r"^\s*system\s*:", re.I | re.M)),
    ("fake_tag", re.compile(r"</?(system|user_input|s)>", re.I)),
    ("markup_smuggle", re.compile(r"<\|.*?\|>|\[INST\]|\[/INST\]")),
    ("base64_run", re.compile(r"[A-Za-z0-9+/]{60,}={0,2}")),
    ("override_hi", re.compile(r"पिछले (सभी )?निर्देश|पुराने निर्देश भूल")),
    (
        "override_hinglish",
        re.compile(
            r"pichle (sabhi )?nirdesh|purane nirdesh bhool|sab bhool jao|instructions bhool", re.I
        ),
    ),
    ("roleplay_hinglish", re.compile(r"\bab tum ho\b|\btum ab ho\b|\backing as\b", re.I)),
)


def heuristic_hits(text: str) -> list[str]:
    return [name for name, pattern in _PATTERNS if pattern.search(text)]


def evaluate(text: str, guard: GuardVerdict | None, *, threshold: float) -> InjectionVerdict:
    hits = heuristic_hits(text)
    if guard is None:
        hit = bool(hits)
        return InjectionVerdict(hit=hit, rule_id=hits[0] if hit else "none", score=None)
    hit = bool(hits) or guard.injection_score >= threshold
    rule_id = hits[0] if hits else ("guard-input" if hit else "none")
    return InjectionVerdict(hit=hit, rule_id=rule_id, score=guard.injection_score)


async def call_guard(
    gateway: Gateway,
    *,
    text: str,
    session_id: UUID,
    turn_id: UUID,
    fsm_state: str,
    response: str | None = None,
) -> GuardVerdict | None:
    """One guard-input call. With `response`, output mode (Step 14): the guard classifies the
    assistant's reply to `text`, as a safety classifier reads an agent turn."""
    messages = [{"role": "user", "content": text}]
    if response is not None:
        messages.append({"role": "assistant", "content": response})
    try:
        result = await gateway.call(
            Route.GUARD_INPUT,
            data_class=DataClass.SELF_HOSTED_RAW,
            messages=messages,
            session_id=session_id,
            turn_id=turn_id,
            fsm_state=fsm_state,
            response_format=GuardVerdict,
        )
    except GatewayUnavailable as exc:
        logger.warning(
            "guard-input unavailable (%s mode): %s",
            "output" if response is not None else "input",
            exc.reason,
        )
        return None
    return result.parsed
