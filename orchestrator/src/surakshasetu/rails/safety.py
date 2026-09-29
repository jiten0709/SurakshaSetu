"""Rail 4 (TDD §4.2): the guard-input safety verdict. Self-harm (S11) is a mandatory trigger that
fails CLOSED: when the guard route is unavailable, a local lexicon hit still routes the turn to the
Safety handler. A degraded-but-clean turn (guard down, no lexicon hit) is not blocked.
"""

import re
from dataclasses import dataclass

from surakshasetu.rails.injection import GuardVerdict

_SELF_HARM_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bkill myself\b", re.I),
    re.compile(r"\bsuicide\b", re.I),
    re.compile(r"\bend my life\b", re.I),
    re.compile(r"\bself[- ]harm\b", re.I),
    re.compile(r"\bwant to die\b", re.I),
    re.compile(r"\bhurt myself\b", re.I),
    re.compile(r"\bno reason to live\b", re.I),
    re.compile(r"आत्महत्या"),
    re.compile(r"मरना चाहता|मरना चाहती"),
    re.compile(r"khud[ -]?kushi", re.I),
    re.compile(r"marna chahta|marna chahti", re.I),
)


@dataclass(frozen=True)
class SafetyVerdict:
    hit: bool
    rule_id: str  # "guard-input" | "self-harm-lexicon" | "none"
    degraded: bool  # the guard route was unavailable


def self_harm_hit(text: str) -> bool:
    return any(pattern.search(text) for pattern in _SELF_HARM_PATTERNS)


def evaluate(text: str, guard: GuardVerdict | None) -> SafetyVerdict:
    if guard is not None:
        hit = guard.safety != "safe"
        return SafetyVerdict(hit=hit, rule_id="guard-input" if hit else "none", degraded=False)
    hit = self_harm_hit(text)
    return SafetyVerdict(hit=hit, rule_id="self-harm-lexicon" if hit else "none", degraded=True)
