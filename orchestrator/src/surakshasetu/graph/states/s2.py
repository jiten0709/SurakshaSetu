"""The S2 node: State-2, needs discovery and suitability (TDD §3.7; Step 20).

S2 asks the slots the pinned rules list (GET /v1/suitability/required-slots, in the DMN's order),
one per turn: the bundle's question and its approved reason, verbatim, led by one empathetic
sentence from gen-converse with the S2 L1 when that is the whole reply (S1's Screen, through its
hooks). Values come only from what the customer said or chose: nlu-extract's evidenced candidates,
or the deterministic reading of the whole message for the open question. Every money answer is read
back: annual flows as "₹9,60,000 a year", lump sums as an amount; "80k" with no period asks a
month or a year, never assumed. Slot rows are appended encrypted with the consent_id (I1).

When every asked slot has a value, the Suitability Service evaluates the needs payload once
(decided 2026-10-04, Q1): the request carries `slots_sha256`, the JCS hash of the needs as sent
(`needs_sha256`, the same vectors as the Java tier), and the result must echo it. The summary is a
template filled from the slots and the result's assumptions (cover-to age, dependency years,
existing cover counted, growth, discount, consumption and final expenses), never paraphrased; an
IMPLAUSIBLE_INPUT result asks the customer to check the figures (Q4). The customer's "yes" binds
`session.needs.slots_sha256` to the result's inputs hash; until then the record is not current, so
no S2 row fires (I2). Then the rows: ESCALATE -> HE (V5), NO_GAP -> Exit (Advisory), and FIT -> S3
once sufficiency reaches the minimum or the customer elects to go on (SUFFICIENCY_ELECTION), and
amber affordability is confirmed. The model never computes cover, affordability or sufficiency.

Edge cases (TDD §3.7, §3.9's S2 column): "just tell me the best plan" offers the three-question
short path (income, dependants, loans; Step 6's defaults for the rest, shown on the summary); a
homemaker or no income gets the non-earning basis in plain words; distress is acknowledged without
sales language, with a pause or an advisor offered, and recorded as financial_distress; "guaranteed
returns" gets no promise; another insurer's plan is not compared; comprehension difficulty is
counted for the vulnerability rules and the question asked again more simply; the Suitability
Service down pauses the session (CC3, Q2), or offers a retry when S2 is being entered.

`correction` is the V4 hook S3 calls: a changed needs fact writes a corrected row, drops the
suitability record and re-runs S2's rows (G3).
"""

import dataclasses
import logging
import re
from decimal import Decimal
from typing import Any, Literal, cast

from langgraph.runtime import Runtime
from pydantic import TypeAdapter, ValidationError

from surakshasetu.analysis.models import Intent
from surakshasetu.analysis.normalisers import devanagari_digits_to_ascii, parse_money, parse_yes_no
from surakshasetu.audit.events import EngineDecisionHeader, EventType, SufficiencyElectionHeader
from surakshasetu.compose.bundle import mentions, phrase
from surakshasetu.compose.placeholders import format_inr
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.domain.client import DomainError
from surakshasetu.domain.models import (
    Dependant,
    EligibilitySnapshot,
    Liability,
    NeedsPayload,
    Occupation,
    Pins,
    RequiredAttribute,
    RequiredSlot,
    SuitabilityRequest,
    SuitabilityResult,
)
from surakshasetu.graph.handlers import append, bundle, quick_reply, scripts, session
from surakshasetu.graph.state import EligibilityPayload, GraphState, SessionState
from surakshasetu.graph.states import s1
from surakshasetu.graph.states.s1 import Answer, Screen, said, valid
from surakshasetu.store import conv as store
from surakshasetu.store.conv import SessionRow

logger = logging.getLogger(__name__)

Kind = Literal["goals", "flow", "lump", "income_type", "dependants", "liabilities"]
# How each needs slot is read. A flow is a yearly amount (read back "a year"; "80k" alone asks the
# period); a lump sum is an amount as stated.
NEEDS: dict[str, Kind] = {
    "goals": "goals",
    "annual_income_inr": "flow",
    "income_type": "income_type",
    "dependants": "dependants",
    "liabilities": "liabilities",
    "existing_cover_inr": "lump",
    "employer_cover_inr": "lump",
    "existing_annual_premium_inr": "flow",
    "earmarked_assets_inr": "lump",
    "premium_budget_inr_pa": "flow",
}
SHORT = frozenset({"annual_income_inr", "dependants", "liabilities"})  # TDD §3.7's short path
# Declined: a nullable slot is sent as null (it counts as declined); the others are left out (not
# answered), except the required existing premiums, sent as "0". Step 6's short-path defaults
# (content/testvectors/needs/02-short-path.json) fill the required slots the short path skips.
NULLABLE = frozenset({"annual_income_inr", "earmarked_assets_inr", "premium_budget_inr_pa"})
DEFAULTS: dict[str, Any] = {
    "goals": ["income_protection"],
    "income_type": "salaried",
    "existing_annual_premium_inr": "0",
}
DISTRESS = "financial_distress"  # a vulnerability input, recorded as a slot row, never asked
NON_EARNING = frozenset({"homemaker", "student", "retired"})
# Slot names a model may use for ours.
ALIASES = {
    "income": "annual_income_inr",
    "annual_income": "annual_income_inr",
    "dependents": "dependants",
    "loans": "liabilities",
    "loan": "liabilities",
    "existing_cover": "existing_cover_inr",
    "employer_cover": "employer_cover_inr",
    "existing_premium": "existing_annual_premium_inr",
    "existing_premiums": "existing_annual_premium_inr",
    "earmarked_assets": "earmarked_assets_inr",
    "assets": "earmarked_assets_inr",
    "premium_budget": "premium_budget_inr_pa",
    "budget": "premium_budget_inr_pa",
}
# Words answers arrive as (EN, Hinglish, Hindi). nlu-extract is told the slot names and usually
# canonicalises; quick replies always do.
GOAL_WORDS = {
    "income_protection": ("income", "family", "protect", "protection", "parivar"),
    "loan_cover": ("loan", "loans", "emi", "debt"),
    "child_education": ("education", "school", "college", "studies", "padhai"),
    "retirement": ("retire", "retirement", "pension"),
    "savings": ("saving", "savings", "invest", "investment"),
}
INCOME_WORDS = (  # self-employed before salaried: "self employed" also says "employed"
    ("self_employed", ("self employed", "self-employed", "business", "freelance", "freelancer")),
    ("salaried", ("salaried", "salary", "job", "employed", "service", "naukri")),
    ("homemaker", ("homemaker", "home maker", "housewife", "house wife", "househusband")),
    ("student", ("student", "studying")),
    ("retired", ("retired", "pensioner")),
)
RELATIONS = {
    **dict.fromkeys(("wife", "husband", "spouse", "partner", "patni", "pati"), "spouse"),
    **dict.fromkeys(
        ("son", "daughter", "child", "kid", "baby", "beta", "beti", "bachcha"), "child"
    ),
    **dict.fromkeys(("mother", "father", "mom", "dad", "mum", "parent", "maa", "papa"), "parent"),
    **dict.fromkeys(
        ("brother", "sister", "grandmother", "grandfather", "nephew", "niece", "mother-in-law",
         "father-in-law", "uncle", "aunt"),
        "other",
    ),
}  # fmt: skip
LOAN_KINDS = (
    ("home", ("home", "house", "housing", "flat")),
    ("vehicle", ("car", "vehicle", "bike", "two-wheeler")),
    ("education", ("education", "student")),
    ("personal", ("personal",)),
    ("business", ("business",)),
)
NONE = frozenset(
    {
        "none", "no", "nil", "zero", "0", "nothing", "no one", "nobody", "no income", "no loans",
        "no loan", "no cover", "not any", "koi nahi", "kuch nahi", "कोई नहीं", "कुछ नहीं",
    }
)  # fmt: skip
_MONTH = re.compile(r"\b(?:month|monthly|months|per month|mahina|mahine|महीना|महीने)\b", re.I)
_YEAR = re.compile(r"\b(?:year|yearly|annual|annually|per annum|pa|lpa|saal|साल)\b", re.I)
_K = re.compile(r"(\d+(?:\.\d+)?)\s*k\b", re.I)
_LOAN_YEARS = re.compile(r"(\d{1,2})\s*(?:more\s+)?(?:years?|yrs?|saal|साल)", re.I)
_DEPENDANTS = TypeAdapter(list[Dependant])
_LIABILITIES = TypeAdapter(list[Liability])
MONTHS_PER_YEAR = 12


@dataclasses.dataclass(frozen=True)
class Period:
    """A yearly amount said as "80k" with no period: a month or a year is asked, never assumed."""

    amount: int


class Unavailable(Exception):
    """The needs record cannot be bound: the result's inputs hash is not the needs hash (I2)."""


def needs_sha256(needs: NeedsPayload) -> str:
    """SHA-256(JCS(the needs object as the client sends it, minus slots_sha256)): the contract's
    inputs_sha256 for evaluateSuitability, and the slot hash I2 binds. The client dumps with
    exclude_unset, so a slot never set is absent, and an explicit null stays (declined)."""
    body = needs.model_dump(mode="json", exclude_unset=True)
    body.pop("slots_sha256", None)
    return sha256_hex(body)


def percent(rate: str) -> str:
    return f"{(Decimal(rate) * 100).normalize():f}%"


# --- understanding one value ----------------------------------------------------------------------
def _folded(text: str) -> str:
    return phrase(devanagari_digits_to_ascii(text))


def _money(text: str, *, flow: bool) -> Answer | Period | None:
    folded = _folded(text)
    yes_no = parse_yes_no(folded)
    if yes_no == "declined":
        return Answer(None, declined=True)
    if folded in NONE or yes_no == "no":
        return Answer("0")
    said_ = devanagari_digits_to_ascii(text)
    month, year = bool(_MONTH.search(said_)), bool(_YEAR.search(said_))
    money = parse_money(said_)
    if money is not None and money.annual_inr is not None:
        # "1 lakh a month": the normaliser reads lakh, crore and grouped digits as stated.
        monthly = flow and month and not money.needs_confirmation
        amount = money.annual_inr * (MONTHS_PER_YEAR if monthly else 1)
    elif k := _K.search(said_):
        amount = round(float(k.group(1)) * 1000)
        if flow and month:
            amount *= MONTHS_PER_YEAR
        elif flow and not year:
            return Period(amount)
    else:
        numbers = re.findall(r"\d+(?:\.\d+)?", said_.replace(",", ""))
        if len(numbers) != 1 or len(said_) > 60:
            return None
        amount = round(float(numbers[0])) * (MONTHS_PER_YEAR if flow and month else 1)
    return Answer(str(amount), derived=amount > 0)  # a non-zero amount is always read back


def _goals(raw: Any, text: str) -> Answer | None:
    goals = tuple(GOAL_WORDS)
    if isinstance(raw, list) and raw and all(g in goals for g in raw):
        return Answer(list(dict.fromkeys(raw)))
    if isinstance(raw, str) and raw in goals:
        return Answer([raw])
    folded = _folded(text)
    if parse_yes_no(folded) == "declined":
        return Answer([], declined=True)
    found = sorted(
        (m.start(), goal)
        for goal, words in GOAL_WORDS.items()
        for word in words
        if (m := re.search(rf"\b{word}\b", folded))
    )
    ranked = list(dict.fromkeys(goal for _, goal in found))  # in the order the customer said them
    return Answer(ranked) if ranked else None


def _income_type(text: str) -> Answer | None:
    folded = _folded(text).replace("_", " ")
    for value, words in INCOME_WORDS:
        if folded == value.replace("_", " ") or any(re.search(rf"\b{w}\b", folded) for w in words):
            return Answer(value)
    return None


def _dependants(raw: Any, text: str) -> Answer | None:
    if isinstance(raw, list):
        try:
            return Answer([d.model_dump(mode="json") for d in _DEPENDANTS.validate_python(raw)])
        except ValidationError:
            return None
    folded = _folded(text)
    yes_no = parse_yes_no(folded)
    if yes_no == "declined":
        return Answer(None, declined=True)
    if folded in NONE or yes_no == "no":
        return Answer([])
    found: list[dict[str, Any]] = []
    relation: str | None = None
    for token in re.findall(r"[a-z\-]+|\d{1,3}", folded):  # each relation, then its age
        if token.isdigit():
            if relation is not None:
                found.append({"relation": relation, "age": int(token)})
                relation = None
        elif token in RELATIONS or token.rstrip("s") in RELATIONS:
            if relation is not None:
                return None  # a relation without an age
            relation = RELATIONS.get(token) or RELATIONS[token.rstrip("s")]
    if relation is not None or not found:
        return None
    try:
        return Answer([d.model_dump(mode="json") for d in _DEPENDANTS.validate_python(found)])
    except ValidationError:
        return None


def _liabilities(raw: Any, text: str) -> Answer | None:
    if isinstance(raw, list):
        loans = [
            {**loan, "outstanding_inr": str(loan["outstanding_inr"])}
            if isinstance(loan, dict) and isinstance(loan.get("outstanding_inr"), int | float)
            else loan
            for loan in raw
        ]
        try:
            valid_ = [loan.model_dump(mode="json") for loan in _LIABILITIES.validate_python(loans)]
        except ValidationError:
            return None
        return Answer(valid_, derived=bool(valid_))
    folded = _folded(text)
    yes_no = parse_yes_no(folded)
    if yes_no == "declined":
        return Answer(None, declined=True)
    if folded in NONE or yes_no == "no":
        return Answer([])
    kind = next(
        (k for k, words in LOAN_KINDS if any(re.search(rf"\b{w}\b", folded) for w in words)),
        "other" if re.search(r"\bloans?\b", folded) else None,
    )
    said_ = devanagari_digits_to_ascii(text)
    years = _LOAN_YEARS.search(said_)
    if kind is None or years is None:
        return None
    money = parse_money(said_)
    if money is not None and money.annual_inr is not None:
        amount = money.annual_inr
    elif k := _K.search(said_):
        amount = round(float(k.group(1)) * 1000)
    else:
        others = [n for n in re.findall(r"\d+", said_.replace(",", "")) if n != years.group(1)]
        if len(others) != 1:
            return None
        amount = int(others[0])
    try:
        loan = Liability(kind=kind, outstanding_inr=str(amount), years_left=int(years.group(1)))
    except ValidationError:
        return None
    return Answer([loan.model_dump(mode="json")], derived=True)


# --- the needs discovery --------------------------------------------------------------------------
@dataclasses.dataclass
class Needs(Screen):
    """S1's Screen over the needs slots: the rules' required slots (asked in their order), the
    newest row per slot (S1's too), and the reply being built."""

    required: list[RequiredSlot] = dataclasses.field(default_factory=list)
    period: tuple[str, int] | None = None  # a yearly amount said without a period
    offer_short: bool = False
    rephrased: bool = False
    distressed_now: bool = False  # distress stated this turn: acknowledged, an answer still read

    @classmethod
    async def open(cls, turn: Any, current: SessionState) -> "Needs":
        required = await turn.domain.get_required_slots(current.pins.rules)
        if unknown := sorted({r.slot for r in required} - set(NEEDS)):
            raise RuntimeError(f"the rules ask slots S2 cannot read: {unknown}")
        attrs = [
            RequiredAttribute(attribute=r.slot, reason_line_id=r.reason_line_id) for r in required
        ]
        row = cast(SessionRow, turn.row)
        known = store.latest_slots(turn.conn, turn.keys, row.key_ref, turn.session_id)
        for slot_row in turn.slot_rows:
            known[slot_row.slot] = (slot_row.status, slot_row.value)
        return cls(turn, current, "s2", attrs, known, required=required)

    # --- Screen's hooks ---------------------------------------------------------------------------
    @property
    def kinds(self) -> dict[str, Any]:
        return NEEDS

    @property
    def aliases(self) -> dict[str, str]:
        return ALIASES

    @property
    def names(self) -> dict[str, str]:
        return scripts(self.turn).needs.names

    @property
    def short(self) -> bool:
        return bool(self.session.counters.get("short_path"))

    @property
    def asked(self) -> list[RequiredAttribute]:
        """The short path asks income, dependants and loans, and keeps whatever was answered."""
        return [
            a
            for a in self.attrs
            if not self.short or a.attribute in SHORT or a.attribute in self.known
        ]

    def invalidate(self, slot: str, status: str) -> None:
        if status != "confirmed" and (slot in NEEDS or slot == DISTRESS):
            self.drop()

    def drop(self) -> None:
        """A needs input changed: the held summary and its suitability record no longer match."""
        self.session.needs = None
        self.session.suitability = None

    async def understand(
        self, slot: str, raw: Any, evidence: str
    ) -> Answer | list[Occupation] | None:
        kind = NEEDS[slot]
        text = raw if isinstance(raw, str) else evidence
        result: Answer | Period | None
        if kind in ("flow", "lump"):
            flow = kind == "flow"
            result = _money(text, flow=flow)
            if isinstance(raw, int | float) and not isinstance(raw, bool) and result is None:
                # nlu-extract's number, when its evidence span says no more (the period stands)
                result = Answer(str(int(raw)), derived=raw > 0) if raw >= 0 else None
        elif kind == "goals":
            result = _goals(raw, text)
        elif kind == "income_type":
            result = _income_type(text)
        elif kind == "dependants":
            result = _dependants(raw, text)
        else:
            result = _liabilities(raw, text)
        if isinstance(result, Period):
            self.period, self.noted = (slot, result.amount), True
            return None
        return result

    async def take(self, slot: str, raw: Any, evidence: str, confidence: float) -> bool:
        filled = await super().take(slot, raw, evidence, confidence)
        value = self.known.get(slot, (None, None))[1]
        if filled and (
            (slot == "income_type" and value in NON_EARNING)
            or (slot == "annual_income_inr" and value == "0")
        ):
            self.lead_once("non_earning_basis", scripts(self.turn).non_earning_basis)
        return filled

    def lead_once(self, part_id: str, text: str) -> None:
        if all(i != part_id for i, _ in self.lead):
            self.lead.append((part_id, text))

    def notes(self) -> None:
        """What the turn is, beyond an answer (TDD §3.7's edge cases, §3.9's S2 column)."""
        turn = self.turn
        pipeline = turn.pipeline
        if pipeline is None:
            return
        texts, lexicon = scripts(turn), bundle(turn).needs_lexicon
        text = pipeline.stored_raw
        intents = set(pipeline.analysis.intents) if pipeline.analysis else set()
        question = "?" in text or bool(intents & s1.QUESTIONS)
        if pipeline.blocked or turn.identity:
            self.noted = True  # slots discarded (injection) or compose answers (I6): nothing else
            return
        # Step 22: a side question or an objection ("guaranteed returns?", another insurer's plan)
        # is answered by the side-query subgraph or the objection handler ahead of this prompt; a
        # language switch asks the open question again in the new language.
        handled = turn.routed in ("side_query", "objection") or turn.language_switched
        if handled:
            self.noted = True
        if s1.pushback(turn, text):  # TDD §3.9: the suitability purpose, then the question again
            self.note("suitability_purpose", texts.suitability_purpose)
            turn.answered = True
        if Intent.FINANCIAL_DISTRESS in intents or mentions(lexicon.distress, text):
            self.distressed()
        if Intent.COMPREHENSION_DIFFICULTY in intents or mentions(lexicon.comprehension, text):
            self.confused()
        if (
            (Intent.EXPRESS_PATH in intents or mentions(lexicon.short_path, text))
            and not self.short
            and not self.session.counters.get("short_offered")
        ):
            self.offer_short, self.noted = True, True
        if Intent.OFF_TOPIC in intents:
            self.note("redirect", texts.redirect)
        if question and not (
            self.lead or self.filled or self.offer_short or self.rephrased or handled
        ):
            self.note("side_query_caveat", texts.side_query_caveat[self.session.fsm_state.value])

    def distressed(self) -> None:
        """Slow down, no sales language, a pause or an advisor; the flag for the vulnerability
        rules (VULN_FINANCIAL_DISTRESS). Nothing else about it is stored."""
        texts = scripts(self.turn)
        if self.known.get(DISTRESS, (None, None))[1] is not True:
            self.write(DISTRESS, True, "proposed", 1.0)
            logger.info("financial distress stated in S2: the vulnerability input recorded")
        self.lead_once("distress_ack", texts.distress_ack)  # not a note: the answer is still read
        self.distressed_now = True
        if not any(r["action"]["type"] == "HUMAN_REQUEST" for r in self.replies):
            sc = texts.screening
            self.replies.append(quick_reply(sc.advisor, "HUMAN_REQUEST", {}))
            self.replies.append(quick_reply(sc.keep_going, "CONTINUE", {}))

    def confused(self) -> None:
        """Counted for the vulnerability rules (comprehension_difficulty_count, a needs input), and
        the question asked again more simply."""
        counters = self.session.counters
        count = counters.get("comprehension_difficulty", 0) + 1
        self.session.counters = {**counters, "comprehension_difficulty": count}
        self.rephrased, self.noted = True, True
        self.drop()
        logger.info("comprehension difficulty in S2: %d so far", count)

    def distress(self) -> bool:
        return self.known.get(DISTRESS, (None, None))[1] is True

    async def answer(self) -> None:
        await super().answer()
        if self.distressed_now and not self.filled:
            self.noted = True  # a message about distress, not an answer the system could not use

    # --- prompts ----------------------------------------------------------------------------------
    async def next(self) -> bool:
        if self.period is not None:
            self.ask_period(*self.period)
            return False
        if self.offer_short:
            self.offer()
            return False
        return await super().next()

    def ask(self, attr: RequiredAttribute, quick: list[dict[str, Any]] | None = None) -> None:
        if self.rephrased:
            hint = self.template(attr).hint or ""
            self.lead_once("rephrase", f"{scripts(self.turn).rephrase} {hint}".strip())
        super().ask(attr, quick)
        if self.distress():
            self.turn.phrase = None  # no generated sentence once distress is stated

    def choices(self, slot: str) -> list[dict[str, Any]]:
        texts = scripts(self.turn)
        labels, n = texts.labels, texts.needs

        def offer(value: Any, label: str) -> dict[str, Any]:
            return quick_reply(label, "SLOT", {"slot": slot, "value": value})

        if slot == "goals":
            return [offer(goal, label) for goal, label in labels.goals.items()]
        if slot == "income_type":
            return [offer(value, label) for value, label in n.income_types.items()]
        if slot == "dependants":
            return [offer("none", n.no_one)]
        if slot == "liabilities":
            return [offer("none", n.no_loans)]
        if slot in ("earmarked_assets_inr", "premium_budget_inr_pa"):
            return [offer("declined", n.skip)]
        return []

    def ask_period(self, slot: str, amount: int) -> None:
        texts = scripts(self.turn)
        self.session.pending_slot = slot
        self.session.last_prompt_id = f"s2.period:{slot}:{amount}"
        self.reply(
            [("period_ask", texts.period_ask.format(amount=format_inr(str(amount))))],
            [
                quick_reply(label, "PERIOD", {"period": period})
                for period, label in texts.needs.period.items()
            ],
        )

    def offer(self) -> None:
        texts = scripts(self.turn)
        n = texts.needs
        self.session.counters = {**self.session.counters, "short_offered": 1}
        self.session.pending_slot = None
        self.session.last_prompt_id = "s2.short"
        self.reply(
            [("short_path_offer", texts.short_path_offer)],
            [
                quick_reply(n.short_yes, "CONFIRM", {"confirmed": True}),
                quick_reply(n.short_no, "CONFIRM", {"confirmed": False}),
            ],
        )
        logger.info("short path offered in S2")

    async def confirm_one(self, slot: str) -> None:
        """A value read back at once: every non-zero amount (TDD §3.7), and 0.6-0.8 confidence."""
        texts = scripts(self.turn)
        self.session.pending_slot = slot
        self.session.last_prompt_id = f"s2.confirm:{slot}"
        _, value = self.known[slot]
        kind = NEEDS[slot]
        if kind == "flow" and value is not None:
            text = texts.money_readback.format(amount=format_inr(value))
        elif kind == "lump" and value is not None:
            text = texts.amount_readback.format(amount=format_inr(value))
        else:
            text = texts.readback.format(facts=self.value_text(slot))
        self.reply([("readback", text)], self._yes_no())

    def value_text(self, slot: str) -> str:
        """How one answer reads in the summary and the read-backs: amounts from the slots only."""
        texts = scripts(self.turn)
        n = texts.needs
        _, value = self.known[slot]
        if value is None:
            return n.declined.get(slot, n.declined["default"])
        kind = NEEDS[slot]
        if kind == "goals":
            return ", ".join(texts.labels.goals[g] for g in value) or n.none
        if kind in ("flow", "lump"):
            if Decimal(value) == 0:
                return n.none
            amount = format_inr(value)
            return n.a_year.format(amount=amount) if kind == "flow" else amount
        if kind == "income_type":
            return n.income_types[value]
        if kind == "dependants":
            return (
                ", ".join(
                    n.dependant.format(relation=n.relations[d["relation"]], age=d["age"])
                    for d in value
                )
                or n.none
            )
        return (
            "; ".join(
                n.loan.format(
                    kind=n.loan_kinds[loan["kind"]],
                    outstanding=format_inr(loan["outstanding_inr"]),
                    years=loan["years_left"],
                )
                for loan in value
            )
            or n.none
        )

    # --- the payload, the decision and the summary ------------------------------------------------
    def payload(self) -> NeedsPayload:
        """The needs as the contract reads them: a slot never asked is left out, so it takes its
        default and does not count toward profile_sufficiency; null counts as declined."""
        known = {slot: value for slot, (_, value) in self.known.items()}
        body: dict[str, Any] = {}
        for slot in NEEDS:
            if slot in known:
                value = known[slot]
                if value is not None or slot in NULLABLE:
                    body[slot] = value
                elif slot == "goals":
                    body[slot] = []
                elif slot in DEFAULTS:
                    body[slot] = DEFAULTS[slot]  # the required premiums: declined counts as none
            elif slot in DEFAULTS:
                body[slot] = DEFAULTS[slot]
        body["financial_distress"] = known.get(DISTRESS) is True
        body["comprehension_difficulty_count"] = self.session.counters.get(
            "comprehension_difficulty", 0
        )
        return NeedsPayload.model_validate(body)

    def snapshot(self) -> EligibilitySnapshot:
        """S1's confirmed facts. Suitability is computed on the life assured's profile."""
        eligibility = cast(EligibilityPayload, self.session.eligibility)
        engine = eligibility.engine
        values = self.values()
        la_age = (
            values.get("proposer.la_age")
            if values.get("proposer.is_life_assured") is False
            else None
        )
        return EligibilitySnapshot(
            age_years=la_age if isinstance(la_age, int) else eligibility.age_years,
            tobacco_12m=eligibility.tobacco_12m,
            gender=eligibility.gender,
            eligible_uins=engine.eligible_uins if engine else [],
            flags=engine.flags if engine else [],
        )

    async def readback(self) -> None:
        """Every asked slot has a value: the Suitability Service decides on the needs as they
        stand (ENGINE_DECISION), and the summary shows them with the engine's assumptions. The
        record stays unbound until the customer confirms (Q1)."""
        turn, current = self.turn, self.session
        lost = current.eligibility is None or current.eligibility.engine is None
        if lost and not await s1.recompute(turn):  # hydration dropped it; S1's rows if it changed
            turn.signals["correction"] = "eligibility"
            return
        needs = self.payload()
        digest = needs_sha256(needs)
        sent = NeedsPayload.model_validate(
            {**needs.model_dump(mode="json", exclude_unset=True), "slots_sha256": digest}
        )
        request = SuitabilityRequest(
            pins=Pins(rules=current.pins.rules), eligibility=self.snapshot(), needs=sent
        )
        result: SuitabilityResult = await turn.domain.evaluate_suitability(request)
        append(
            turn,
            EventType.ENGINE_DECISION,
            EngineDecisionHeader(
                service="suitability",
                decision_id=str(result.decision_id),
                rules_version=result.rules_version,
                params_version=result.params_version,
                inputs_sha256=result.inputs_sha256,
                reason_codes=result.reason_codes,
            ),
            {
                # exactly as sent, so the inputs hash can be recomputed from the audit record
                "request": request.model_dump(mode="json", exclude_unset=True),
                "result": result.model_dump(mode="json"),
            },
        )
        logger.info(
            "suitability %s: %s, affordability %s, sufficiency %s, reasons %s",
            result.decision_id,
            result.outcome,
            result.affordability,
            result.profile_sufficiency,
            result.reason_codes,
        )
        if result.inputs_sha256 != digest:
            logger.error("I2: suitability inputs_sha256 is not the needs hash; nothing bound")
            self.drop()
            raise Unavailable
        current.needs, current.suitability = needs, result
        self.summary(result)

    def summary(self, result: SuitabilityResult) -> None:
        texts = scripts(self.turn)
        n, a = texts.needs, result.assumptions
        facts = [
            n.facts[attr.attribute].format(value=self.value_text(attr.attribute))
            for attr in self.asked
        ]
        facts.append(
            n.facts["existing_cover_counted"].format(value=format_inr(a.existing_cover_counted_inr))
        )
        text = texts.needs_summary.format(
            facts="\n".join(facts),
            cover_to_age=a.cover_to_age,
            dependency_years=a.dependency_years,
            growth=percent(a.income_growth),
            discount=percent(a.discount_rate),
            consumption=percent(a.consumption_share),
            final_expenses=format_inr(a.final_expenses_inr),
        )
        parts = [("needs_summary", text)]
        asked = {attr.attribute for attr in self.asked}
        if assumed := [n.assumed[s] for s in NEEDS if s not in asked and s in n.assumed]:
            parts.append(
                ("summary_assumed", texts.summary_assumed.format(items="; ".join(assumed)))
            )
        if "IMPLAUSIBLE_INPUT" in result.reason_codes:
            parts.append(("summary_implausible", texts.summary_implausible))
        parts.append(("summary_question", texts.summary_question))
        self.session.pending_slot = None
        self.session.last_prompt_id = "s2.readback"
        self.reply(parts, self._yes_no())

    def unanswered(self) -> list[RequiredAttribute]:
        """Required slots not answered: never asked (the short path), or declined."""
        return [
            a
            for a in self.attrs
            if a.attribute not in self.known or self.known[a.attribute][1] is None
        ]


# --- the rows ------------------------------------------------------------------------------------
def _bind(screen: Needs) -> None:
    """The summary confirmed: the needs record bound to the result's inputs hash (I2), then the
    reply for the row decide will take. The hash is taken again over the needs rebuilt from the
    slots just confirmed (the held copy has been through the checkpoint, which keeps values, not
    which members were sent), so the record binds only if it is still the one for these slots."""
    current = screen.session
    result = cast(SuitabilityResult, current.suitability)
    needs = screen.payload()
    digest = needs_sha256(needs)
    if digest != result.inputs_sha256:
        logger.error("I2: the held needs no longer hash to the suitability record; nothing bound")
        screen.drop()
        raise Unavailable
    current.needs = needs.model_copy(update={"slots_sha256": digest})
    logger.info("needs confirmed: suitability %s bound (%s)", result.decision_id, result.outcome)
    _route(screen, result)


def _route(screen: Needs, result: SuitabilityResult, *, elected: bool = False) -> None:
    """The reply for a bound record. ESCALATE is the HE handler's to answer (V5); NO_GAP exits
    advisory; FIT asks for the election below the minimum, then confirmation of amber, else the
    bridge to S3."""
    turn, texts = screen.turn, scripts(screen.turn)
    partial = result.profile_sufficiency < turn.settings.profile_sufficiency_min
    if result.outcome == "ESCALATE":
        screen.reply([], [])
    elif result.outcome == "NO_GAP":
        screen.reply([("no_gap", texts.no_gap)], [])
    elif partial and not elected:
        _prompt(screen, "s2.elect", ("partial_offer", texts.partial_offer), "elect")
    elif result.affordability == "amber":
        _prompt(screen, "s2.amber", ("amber_confirm", texts.amber_confirm), "amber")
    else:
        _bridge(screen, partial)
    screen.turn.phrase = None


def _prompt(screen: Needs, prompt: str, part: tuple[str, str], kind: str) -> None:
    n, sc = scripts(screen.turn).needs, scripts(screen.turn).screening
    screen.session.pending_slot = None
    screen.session.last_prompt_id = prompt
    if kind == "elect":
        quick = [
            quick_reply(n.elect, "ELECT", {"elected": True}),
            quick_reply(n.answer_more, "ELECT", {"elected": False}),
        ]
    else:
        quick = [
            quick_reply(n.see_options, "CONFIRM", {"confirmed": True}),
            quick_reply(sc.advisor, "HUMAN_REQUEST", {}),
        ]
    screen.reply([part], quick)


def _bridge(screen: Needs, partial: bool) -> None:
    """S2.2 takes the session to S3 this turn; Step 21's S3 labels every option of a partial
    profile, and the bridge says so already."""
    texts = scripts(screen.turn)
    parts = [("needs_done", texts.needs_done)]
    if partial:
        locale = screen.session.locale
        recommendation = bundle(screen.turn).templates[locale].recommendation
        parts.append(("partial_profile", recommendation.partial_profile))
    screen.session.pending_slot = None
    screen.session.last_prompt_id = "s2.done"
    screen.reply(parts, [])


def _choice(turn: Any, action: str, key: str) -> bool | None:
    """A structured yes or no (ELECT, CONFIRM) or a typed one."""
    act = turn.action or {}
    if act.get("type") == action:
        value = (act.get("payload") or {}).get(key)
        return value if isinstance(value, bool) else None
    return said(turn)


async def _prompts(screen: Needs, prompt: str) -> bool:
    """S2's own prompts: the short-path offer, the period of an amount, the election and the amber
    confirmation. True when the turn was the answer to one of them."""
    turn, current = screen.turn, screen.session
    if prompt == "s2.short":
        choice = _choice(turn, "CONFIRM", "confirmed")
        if choice is None:
            return False
        if choice:
            current.counters = {**current.counters, "short_path": 1}
            logger.info("short path taken in S2")
        if await screen.next():
            await screen.readback()
        return True
    if prompt.startswith("s2.period:"):
        return await _period(screen, prompt)
    result = current.suitability
    if result is None or current.needs is None or current.needs.slots_sha256 is None:
        return False  # the record changed since: the open prompt no longer applies
    if prompt == "s2.elect":
        choice = _choice(turn, "ELECT", "elected")
        if choice is None:
            return False
        _elect(screen, result, choice)
        return True
    if prompt == "s2.amber":
        choice = _choice(turn, "CONFIRM", "confirmed")
        if choice is None:
            return False
        if choice:
            turn.signals["amber_confirmed"] = True
            partial = result.profile_sufficiency < turn.settings.profile_sufficiency_min
            if partial:  # the amber prompt follows the election (_route)
                turn.signals["sufficiency_elected"] = True
            logger.info("amber affordability confirmed")
            _bridge(screen, partial)
        else:
            texts = scripts(turn)
            sc = texts.screening
            current.last_prompt_id = "s2.advisor"
            screen.reply(
                [("advisor_offer", texts.advisor_offer)],
                [
                    quick_reply(sc.advisor, "HUMAN_REQUEST", {}),
                    quick_reply(sc.keep_going, "CONTINUE", {}),
                ],
            )
        return True
    return False


async def _period(screen: Needs, prompt: str) -> bool:
    """The answer to "a month or a year?": the amount as said, annualised, then read back."""
    turn = screen.turn
    _, slot, amount = prompt.split(":")
    act = turn.action or {}
    if act.get("type") == "PERIOD":
        period = (act.get("payload") or {}).get("period")
    elif turn.pipeline is not None:
        text = turn.pipeline.stored_raw
        period = "month" if _MONTH.search(text) else "year" if _YEAR.search(text) else None
    else:
        period = None
    if period not in ("month", "year"):
        screen.ask_period(slot, int(amount))
        return True
    value = str(int(amount) * (MONTHS_PER_YEAR if period == "month" else 1))
    prior = screen.known.get(slot)
    screen.write(slot, value, "corrected" if prior else "proposed", 1.0)
    screen.filled.add(slot)
    screen.confirm = slot
    await screen.next()
    return True


def _elect(screen: Needs, result: SuitabilityResult, elected: bool) -> None:
    """The recorded election below the sufficiency minimum (TDD §3.7): go on with a partial
    profile, or answer the remaining questions."""
    turn, current = screen.turn, screen.session
    missing = screen.unanswered()
    append(
        turn,
        EventType.SUFFICIENCY_ELECTION,
        SufficiencyElectionHeader(
            score=result.profile_sufficiency,
            threshold=turn.settings.profile_sufficiency_min,
            missing_slot_count=len(missing),
            elected=elected,
        ),
        {},
    )
    logger.info("sufficiency election: %s, %d slots unanswered", elected, len(missing))
    if elected:
        turn.signals["sufficiency_elected"] = True
        _route(screen, result, elected=True)
        return
    if screen.short:
        current.counters = {**current.counters, "short_path": 0}
    if missing:
        screen.ask(missing[0])


def _down(turn: Any, exc: Exception) -> None:
    """The Suitability Service is down, or its result cannot be bound (TDD §3.9 "Dependency
    down"): pause, save (the slot rows commit with this turn), resume later. A 4xx is a contract
    problem and propagates."""
    if isinstance(exc, DomainError) and exc.status is not None and exc.status < 500:
        raise exc
    code = exc.code if isinstance(exc, DomainError) else "I2_MISMATCH"
    logger.warning("suitability unavailable in S2: %s; pausing", code)
    turn.signals["dependency_down"] = True
    turn.parts, turn.quick_replies, turn.form, turn.phrase = [], [], None, None


# --- State-2 --------------------------------------------------------------------------------------
async def node(state: GraphState, *, runtime: Runtime[Any]) -> None:
    turn = runtime.context
    current = session(turn)
    await s1.correction(turn)  # V4: an eligibility fact corrected here re-runs S1's rows (G2)
    kind = (turn.action or {}).get("type")
    if turn.signals.get("correction") or not valid(current) or kind == "HUMAN_REQUEST":
        return  # G2; G1 re-enters S0 (I1: no call, no row); CC2 hands over
    if turn.pipeline is not None and turn.pipeline.overlong:
        return  # compose asks to shorten; the question stands
    prompt = current.last_prompt_id or ""
    try:
        screen = await Needs.open(turn, current)
        if await _prompts(screen, prompt):
            return
        if await screen.respond():
            confirmed = prompt == "s2.readback" and not screen.filled and said(turn) is True
            if confirmed and current.needs is not None and current.suitability is not None:
                _bind(screen)
            else:
                await screen.readback()
    except (DomainError, Unavailable) as exc:
        _down(turn, exc)


async def enter(state: GraphState, *, runtime: Runtime[Any]) -> None:
    """Another state's turn entered S2 (S1.4, QO.3, G3, S3.3, a resume): the first open question
    after the bridge, or the summary when everything is answered. The transition is decided, so a
    Suitability Service outage here offers a retry rather than a pause."""
    turn = runtime.context
    current = session(turn)
    bridge = list(turn.parts)
    try:
        screen = await Needs.open(turn, current)
        screen.lead = list(bridge)
        if await screen.next():
            await screen.readback()
    except (DomainError, Unavailable) as exc:
        if isinstance(exc, DomainError) and exc.status is not None and exc.status < 500:
            raise
        logger.warning("suitability unavailable entering S2; retry offered")
        turn.parts = [*bridge, ("needs_retry", scripts(turn).needs_retry)]
        turn.quick_replies = [quick_reply(scripts(turn).screening.retry, "RETRY", {})]
        turn.phrase = None
    logger.info("S2 entered: %s", current.last_prompt_id)


# --- V4 from S3 ----------------------------------------------------------------------------------
async def correction(turn: Any) -> None:
    """A changed needs fact in S3 (TDD §3.1 V4): a corrected row, the suitability record dropped,
    and the `correction` signal, so G3 re-runs S2's rows (a new hash, then a new record before S3
    reopens). The S3 node calls this after s1.correction; an eligibility correction comes first."""
    current = session(turn)
    said_ = [(ALIASES.get(c.slot, c.slot), c) for c in turn.slots_pending]
    said_ = [(slot, c) for slot, c in said_ if slot in NEEDS]
    if not valid(current) or not said_ or turn.signals.get("correction"):
        return
    screen = await Needs.open(turn, current)
    before = dict(screen.known)
    for slot, candidate in said_:
        await screen.take(slot, candidate.value, candidate.evidence_span, candidate.confidence)
    if screen.known != before:
        turn.signals["correction"] = "needs"
        logger.info("needs fact corrected in %s: S2's rows again", current.fsm_state)
