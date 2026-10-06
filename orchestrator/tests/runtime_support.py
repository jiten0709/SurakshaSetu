"""Shared by the runtime tests (unit and db): the model routes and the domain tier behind
httpx.MockTransport (no network), an in-memory stand-in for the Redis gate, the dev pins, and (db
tests only) a real Runtime over the test database."""

import hashlib
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import yaml
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, ConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError

from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import canonical_json, sha256_hex
from surakshasetu.crypto.keys import KeyService
from surakshasetu.domain.client import DomainClient
from surakshasetu.domain.models import DisclosureItem, DisclosureSet
from surakshasetu.gateway import Gateway
from surakshasetu.graph.nodes import build_graph
from surakshasetu.graph.runtime import Runtime
from surakshasetu.graph.state import VersionPins
from surakshasetu.kb.payload import content_sha256
from surakshasetu.rails.output import load_pack

GATEWAY = "http://gateway.test/v1"
DOMAIN = "http://domain.test"
NOTICE = "2026.09.1-en"


def settings(**update: Any) -> Settings:
    return Settings(_env_file=None, gateway_base_url=GATEWAY, **update)


def pins() -> VersionPins:
    return VersionPins(
        prompt_bundle="pb-2026.10.6",
        rules="2026.09.1",
        corpus={},
        consent_notice=NOTICE,
        params="actuarial-dummy-2026.09.1",
        ranker="ranker-2026.09.1",
        registry="2026.09.1",
    )


class Models:
    """guard-input, nlu-extract and verify-claims. `intents`/`side_query`/`slots` shape the
    analysis; `safety`/`injection` the guard's verdict; `down` names routes that fail."""

    def __init__(
        self,
        *,
        intents: tuple[str, ...] = (),
        side_query: str | None = None,
        safety: str = "safe",
        injection: float = 0.01,
        down: tuple[str, ...] = (),
        slots: tuple[dict[str, Any], ...] = (),
        converse: tuple[str, ...] = (),
        recommend: tuple[str, ...] = (),
    ) -> None:
        self.intents, self.side_query, self.slots = intents, side_query, slots
        self.converse = list(converse)  # gen-converse drafts, in order; then FRIENDLY
        self.recommend = list(recommend)  # Step 21: gen-recommend drafts, in order; then CITED
        self.safety, self.injection, self.down = safety, injection, down
        self.routes: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        route = json.loads(request.content)["model"]
        self.routes.append(route)
        if route in self.down:
            raise httpx.ConnectError("down")
        if route == "guard-input":
            answer: dict[str, Any] = {"injection_score": self.injection, "safety": self.safety}
        elif route == "nlu-extract":
            answer = {
                "intents": list(self.intents),
                "slots": list(self.slots),
                "side_query": self.side_query,
                "language": "en",
            }
        elif route == "verify-claims":
            answer = {"verdict": "entailed"}
        elif route == "gen-converse":  # Step 19: S1's one friendly sentence
            return completion(route, self.converse.pop(0) if self.converse else FRIENDLY)
        elif route == "gen-recommend":  # Step 21: S3's narrative and cited answers
            return completion(route, self.recommend.pop(0) if self.recommend else CITED)
        else:
            raise AssertionError(f"unexpected route {route}")
        return completion(route, json.dumps(answer))


FRIENDLY = "Thanks, that helps me understand what you are looking for."
# Step 21: a narrative citing the first option's engine fact (always issued in S3), no number.
CITED = "This option fits the needs you confirmed [R1]."


def completion(route: str, content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"model": f"stub-{route}", "choices": [{"message": {"content": content}}]}
    )


def gateway(models: Models, config: Settings | None = None) -> Gateway:
    return Gateway(config or settings(), httpx.MockTransport(models))


VERSIONS = {
    "rules_version": "2026.09.1",
    "params_version": "actuarial-dummy-2026.09.1",
    "ranker_version": "ranker-2026.09.1",
    "registry_version": "2026.09.1",
    "rating_version": "rating-dummy-2026.09.1",
    "active_rules_versions": ["2026.09.1"],
}


def notice_json(language: str = "en-IN") -> dict[str, Any]:
    body = f"DUMMY notice ({language})"
    return {
        "notice_version": NOTICE if language == "en-IN" else NOTICE.replace("-en", "-hi"),
        "language": language,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "is_dummy": True,
    }


def ai_disclosure_json(language: str = "en-IN") -> dict[str, Any]:
    body = f"DUMMY: AI assistant identity and human alternative ({language})."
    return {
        "disclosure_id": "DISC-GLOBAL-AI-06",
        "language": language,
        "body": body,
        "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "is_dummy": True,
    }


def domain_handler(request: httpx.Request) -> httpx.Response:
    """The reference reads: the two a session needs at creation, the S0 prompt's notice and AI
    disclosure (Step 18), and S1's and Quote-Only's calls (Step 19, `screening_handler`)."""
    language = request.url.params.get("language", "en-IN")
    if request.url.path == "/v1/meta/versions":
        return httpx.Response(200, json=VERSIONS)
    if request.url.path == "/v1/consent/notices/current":
        return httpx.Response(200, json=notice_json(language))
    if request.url.path == "/v1/disclosures/DISC-GLOBAL-AI-06":
        return httpx.Response(200, json=ai_disclosure_json(language))
    return screening_handler(request)


# --- Step 19: a stand-in for the domain tier behind S1 and Quote-Only -----------------------------
# The DMN's RequiredAttributes rows in order (gender is never asked: no DUMMY product rates by it),
# a few seed pincodes and occupations, the three seed products, and a small imitation of the
# eligibility rules and the quote adapter's problems. The real ones run in the golden suites.
REQUIRED_ATTRIBUTES = [
    {"attribute": a, "reason_line_id": r, "asked_if": c}
    for a, r, c in (
        ("age_years", "RL-S1-AGE", None),
        ("residency", "RL-S1-RESIDENCY", None),
        ("pincode", "RL-S1-PINCODE", None),
        ("tobacco_12m", "RL-S1-TOBACCO", None),
        ("occupation_code", "RL-S1-OCCUPATION", None),
        ("health_flags", "RL-S1-HEALTH", None),
        ("proposer.is_life_assured", "RL-S1-PROPOSER", None),
        ("proposer.relationship", "RL-S1-LA-RELATIONSHIP", "proposer.is_life_assured = false"),
        ("proposer.la_age", "RL-S1-LA-AGE", "proposer.is_life_assured = false"),
        ("proposer.business_cover", "RL-S1-BUSINESS-COVER", "proposer.is_life_assured = false"),
    )
]
PINCODES = {"411001": ("Pune", True), "411014": ("Pune", True), "744101": ("South Andaman", False)}
OCCUPATIONS = [
    {"code": "OCC-OFFICE-01", "label": "Salaried office professional", "risk_class": 1},
    {"code": "OCC-TEACH-02", "label": "Teacher or lecturer", "risk_class": 1},
    {"code": "OCC-SWE-03", "label": "Software engineer", "risk_class": 1},
    {"code": "OCC-SALES-04", "label": "Field sales executive", "risk_class": 2},
]
TERM, ROP, SAVER = "999N001V02", "999N002V01", "999N010V01"
PRODUCTS = {
    TERM: ("Suraksha Term Shield", "TERM", 18, 65, "10000000", 30, True),
    ROP: ("Suraksha Term Shield ROP", "TERM_ROP", 18, 55, "5000000", 25, True),
    SAVER: ("Suraksha Saver Guarantee", "NON_PAR_SAVINGS", 18, 55, "1000000", 15, False),
}
SIMPLE = {"self", "spouse", "child", "parent"}
# Step 21: the seed riders (name, DUMMY rate per 1,000) and the products they attach to.
RIDERS = {
    "999A007V01": ("Accidental death benefit", 0.30),
    "999A008V01": ("Waiver of premium", 0.15),
    "999A009V01": ("Critical illness", 0.80),
}
PRODUCT_RIDERS = {TERM: ["999A007V01", "999A008V01", "999A009V01"], ROP: ["999A007V01"]}


def document_json(uin: str, kind: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "version": "v2",
        "language": "en-IN",
        "uri": f"content/seed/kb/product/{uin}-{kind.lower()}-v2.md",
        "sha256": hashlib.sha256(f"{uin}:{kind}:v2".encode()).hexdigest(),
    }


def problem(status: int, code: str, **extra: Any) -> httpx.Response:
    body = {"type": "about:blank", "title": code, "status": status, "code": code, **extra}
    return httpx.Response(status, json=body)


def product_json(uin: str) -> dict[str, Any]:
    name, category, age_min, age_max, cover, term, launched = PRODUCTS[uin]
    return {
        "uin": uin,
        "name": name,
        "category": category,
        "status": "in_force",
        "entry_age_min": age_min,
        "entry_age_max": age_max,
        "maturity_age_max": 85,
        "sa_min_inr": "2500000.00",
        "sa_max_inr": "100000000.00",
        "term_years_min": 10,
        "term_years_max": 40,
        "ppt_options": ["regular"],
        "benefit_payment_options": ["lumpsum"],
        "rider_uins": PRODUCT_RIDERS.get(uin, []),
        "effective_from": "2026-09-01",
        "effective_to": None,
        "launch_enabled": launched,
        "is_dummy": True,
        "riders": [
            {
                "uin": r,
                "name": RIDERS[r][0],
                "attaches_to": [TERM, ROP],
                "sa_max_inr": None,
                "is_dummy": True,
            }
            for r in PRODUCT_RIDERS.get(uin, [])
        ],
        "documents": [document_json(uin, kind) for kind in ("CIS", "POLICY_WORDING")],
        "quote_defaults": {
            "sum_assured_inr": cover,
            "term_years": term,
            "ppt": "regular",
            "frequency": "annual",
            "rider_uins": [],
        },
    }


def eligibility_json(body: dict[str, Any]) -> dict[str, Any]:
    """The DMN's precedence, roughly: minor, NRI, age band, complex proposer, occupation,
    pincode."""
    age, proposer = body["age_years"], body["proposer"]
    complex_ = not proposer["is_life_assured"] and (
        proposer.get("relationship") not in SIMPLE or bool(proposer.get("business_cover"))
    )
    outcome, reason = "ELIGIBLE", None
    if age < 18:
        outcome = "DATA_ERASURE_EXIT"
    elif body["residency"] != "resident":
        outcome, reason = "HUMAN_ESCALATION", "HE_NRI"
    elif age > 65:
        outcome, reason = "HUMAN_ESCALATION", "HE_AGE_BAND"
    elif complex_:
        outcome, reason = "HUMAN_ESCALATION", "HE_COMPLEX_PROPOSER"
    elif body["occupation_code"] is None:
        outcome = "RE_ASK"
    elif not PINCODES.get(body["pincode"], ("", False))[1]:
        outcome = "NOT_ELIGIBLE"
    health = body["health_flags"].values()
    flags = (["MEDICAL_UW"] if True in health else []) + (
        ["PREMIUM_WITHHELD"] if body["tobacco_12m"] is None or None in health else []
    )
    return {
        "decision_id": "0199a1b2-0000-7000-8000-00000000e11e",
        "outcome": outcome,
        "eligible_uins": [TERM, ROP] if outcome == "ELIGIBLE" else [],
        "uw_path": "manual" if "MEDICAL_UW" in flags else "standard",
        "flags": flags,
        "escalation_reason": reason,
        "rule_ids": ["E-01"],
        "reason_codes": ["REASON_PIN_UNSERVICEABLE"] if outcome == "NOT_ELIGIBLE" else [],
        "rules_version": "2026.09.1",
        "params_version": "actuarial-dummy-2026.09.1",
        "inputs_sha256": hashlib.sha256(json.dumps(body).encode()).hexdigest(),
    }


def quote_json(body: dict[str, Any]) -> httpx.Response:
    """The quote adapter's order of checks, for the seed products: tobacco, launch, entry age,
    cover range and the 5 lakh step. ₹16 per ₹10,000 of cover (the DUMMY ₹16,000 for ₹1 crore)."""
    uin, cover, age = body["uin"], int(float(body["sum_assured_inr"])), body["age_years"]
    if body["tobacco_12m"] is None:
        return problem(422, "TOBACCO_UNDISCLOSED")
    _, _, age_min, age_max, _, _, launched = PRODUCTS[uin]
    if not launched:
        return problem(409, "PRODUCT_WITHDRAWN")
    if not age_min <= age <= age_max:
        bounds = {"allowed_min": str(age_min), "allowed_max": str(age_max)}
        return problem(422, "QUOTE_OUT_OF_BOUNDS", field="age_years", **bounds)
    if not 2_500_000 <= cover <= 100_000_000 or cover % 500_000:
        bounds = {"allowed_min": "2500000", "allowed_max": "100000000", "allowed_step": "500000"}
        return problem(422, "QUOTE_OUT_OF_BOUNDS", field="sum_assured_inr", **bounds)
    return httpx.Response(200, json=priced(body))


def priced(body: dict[str, Any], valid_until: str = "2026-11-03") -> dict[str, Any]:
    """The quote for a request the adapter accepted: ₹16 per ₹10,000 of cover, plus each rider at
    its DUMMY rate per ₹1,000 (Step 21)."""
    cover = int(float(body["sum_assured_inr"]))
    riders = {r: f"{round(cover / 1000 * RIDERS[r][1])}" for r in body.get("rider_uins", [])}
    base = cover * 16 // 10_000
    return {
        "decision_id": "0199a1b2-0000-7000-8000-0000000004a0",
        "quote_id": f"Q-2026-10-04-{abs(hash(json.dumps(body, sort_keys=True))) % 10_000:04d}",
        "uin": body["uin"],
        "sum_assured_inr": str(cover),
        "term_years": body["term_years"],
        "ppt": body["ppt"],
        "annual_premium_inr": f"{base + sum(int(v) for v in riders.values())}",
        "frequency": body["frequency"],
        "valid_until": valid_until,
        "indicative": True,
        "rider_premiums": riders,
        "gst_included": True,
        "rating_version": "rating-dummy-2026.09.1",
        "inputs_sha256": hashlib.sha256(json.dumps(body).encode()).hexdigest(),
        "reason_codes": [],
    }


# --- Step 20: a stand-in for the Suitability Service behind S2 ------------------------------------
# The DMN's RequiredSlots rows in order, with their sufficiency weights, and an imitation of the
# evaluation: the contract's inputs hash (JCS over the needs as sent, minus slots_sha256), presence
# for sufficiency (a null or empty goals is unanswered), and FIT unless a test says otherwise. The
# real engine runs in the golden suites.
REQUIRED_SLOTS = [
    {"slot": slot, "weight": weight, "reason_line_id": reason}
    for slot, weight, reason in (
        ("goals", 0.15, "RL-S2-GOALS"),
        ("annual_income_inr", 0.25, "RL-S2-INCOME"),
        ("income_type", 0.05, "RL-S2-INCOME-TYPE"),
        ("dependants", 0.20, "RL-S2-DEPENDANTS"),
        ("liabilities", 0.15, "RL-S2-LIABILITIES"),
        ("existing_cover_inr", 0.05, "RL-S2-EXISTING-COVER"),
        ("employer_cover_inr", 0.05, "RL-S2-EMPLOYER-COVER"),
        ("existing_annual_premium_inr", 0.05, "RL-S2-EXISTING-PREMIUM"),
        ("earmarked_assets_inr", 0.025, "RL-S2-ASSETS"),
        ("premium_budget_inr_pa", 0.025, "RL-S2-BUDGET"),
    )
]
ASSUMPTIONS = {
    "cover_to_age": 60,
    "dependency_years": 26,
    "discount_rate": "0.07",
    "income_growth": "0.05",
    "consumption_share": "0.30",
    "final_expenses_inr": "200000",
    "existing_cover_counted_inr": "750000",
}


def suitability_json(body: dict[str, Any], **result: Any) -> dict[str, Any]:
    needs = {k: v for k, v in body["needs"].items() if k != "slots_sha256"}
    weights = {r["slot"]: r["weight"] for r in REQUIRED_SLOTS}
    answered = [s for s in weights if needs.get(s) is not None and needs.get(s) != [] or (
        s == "dependants" and needs.get(s) == [])]  # fmt: skip
    income = needs.get("annual_income_inr")
    return {
        "decision_id": "0199a1b2-0000-7000-8000-00000000500a",
        "outcome": "FIT",
        "profile_sufficiency": round(sum(weights[s] for s in answered), 3),
        "affordability_premium_estimate_inr": "60000",
        "fit_types": ["TERM", "TERM_ROP"],
        "excluded": {},
        "need_inr": "37147714.43",
        "recommended_cover_inr": "37500000",
        "uw_cap_inr": None if income is None else "60000000",
        "term_years": 26,
        "affordability": "unknown" if income in (None, "0") else "green",
        "vulnerability_flags": [],
        "assumptions": ASSUMPTIONS,
        "rule_ids": ["FIT-01"],
        "reason_codes": [],
        "params_version": "actuarial-dummy-2026.09.1",
        "rules_version": "2026.09.1",
        "inputs_sha256": hashlib.sha256(canonical_json(needs)).hexdigest(),
    } | result


# --- Step 21: the ranker, alternatives and the registry behind S3 ---------------------------------
# An imitation of the ranker over the seed products (the probe's DUMMY shape: the term plan at the
# recommended cover with its rule-fit riders, the ROP plan capped at ₹2 crore), the quote adapter's
# alternatives, and the seed registry sets with the bodies and hashes the registry computes.
SEED_DISCLOSURES = Path(__file__).resolve().parents[2] / "content" / "seed" / "catalog"
COVER = {TERM: "37500000", ROP: "20000000"}


def ranking_json(body: dict[str, Any]) -> dict[str, Any]:
    suitability = body["suitability"]
    excluded = set(body["excluded_uins"])
    withheld = body["tobacco_12m"] is None or "PREMIUM_WITHHELD" in body["flags"]
    candidates = [
        u for u in body["eligible_uins"]
        if u not in excluded and PRODUCTS[u][1] in suitability["fit_types"] and PRODUCTS[u][6]
    ]  # fmt: skip
    options = []
    for rank, uin in enumerate(candidates[:3], start=1):
        cover = COVER.get(uin, "10000000")
        riders = PRODUCT_RIDERS.get(uin, [])
        request = {
            "uin": uin,
            "sum_assured_inr": cover,
            "term_years": suitability["term_years"],
            "ppt": "regular",
            "rider_uins": riders,
            "frequency": "annual",
        }
        gap = max(int(float(suitability["recommended_cover_inr"])) - int(cover), 0)
        options.append(
            {
                "rank": rank,
                "uin": uin,
                "sum_assured_inr": cover,
                "term_years": suitability["term_years"],
                "ppt_years": suitability["term_years"],
                "rider_uins": riders,
                "quote": None if withheld else priced(request, "2026-11-04"),
                "reason_codes": [f"RANK-FIT-{PRODUCTS[uin][1]}"]
                + (["RANK-SA-CAPPED"] if gap else [])
                + (["PREMIUM_WITHHELD"] if withheld else []),
                "protection_gap_inr": str(gap),
            }
        )
    return {
        "decision_id": "0199a1b2-0000-7000-8000-0000000000a1",
        "options": options,
        "ranker_version": "ranker-2026.09.1",
        "suitability_inputs_sha256": suitability["inputs_sha256"],
        "inputs_sha256": hashlib.sha256(canonical_json(body)).hexdigest(),
        "reason_codes": [] if options else ["NO_ELIGIBLE_OPTION"],
    }


def alternatives_json(body: dict[str, Any]) -> list[dict[str, Any]]:
    """LOWER_COVER -25%/-50% and each rider dropped, as the adapter offers them."""
    cover = int(float(body["sum_assured_inr"]))
    recommended = int(float(body["recommended_cover_inr"]))
    variants = [
        ("LOWER_COVER", {**body, "sum_assured_inr": str(cover * 3 // 4)}),
        ("LOWER_COVER", {**body, "sum_assured_inr": str(cover // 2)}),
        *(
            ("FEWER_RIDERS", {**body, "rider_uins": [r for r in body["rider_uins"] if r != drop]})
            for drop in body["rider_uins"]
        ),
    ]
    return [
        {
            "quote": priced(request, "2026-11-04"),
            "protection_gap_inr": str(max(recommended - int(request["sum_assured_inr"]), 0)),
            "change": change,
        }
        for change, request in variants
    ]


def seed_set(uin: str = TERM, language: str = "en-IN", channel: str = "web") -> DisclosureSet:
    """The seed registry set, with bodies and hashes as the registry computes them."""
    seed = yaml.safe_load((SEED_DISCLOSURES / "disclosures.yaml").read_text(encoding="utf-8"))
    bodies = {d["disclosure_id"]: d["bodies"][language] for d in seed["disclosures"]}
    row = next(s for s in seed["disclosure_sets"] if s["uin"] == uin)
    items = [
        DisclosureItem(disclosure_id=i, body=bodies[i], body_sha256=content_sha256(bodies[i]))
        for i in row["disclosure_ids"]
    ]
    payload = {
        "registry_version": row["registry_version"],
        "uin": uin,
        "channel": channel,
        "language": language,
        "items": [{"disclosure_id": i.disclosure_id, "body_sha256": i.body_sha256} for i in items],
    }
    return DisclosureSet(
        uin=uin,
        channel=channel,
        language=language,
        registry_version=row["registry_version"],
        items=items,
        set_sha256=sha256_hex(payload),
        is_dummy=True,
    )


def disclosure_json(disclosure_id: str, language: str) -> dict[str, Any]:
    seed = yaml.safe_load((SEED_DISCLOSURES / "disclosures.yaml").read_text(encoding="utf-8"))
    body = next(d for d in seed["disclosures"] if d["disclosure_id"] == disclosure_id)["bodies"][
        language
    ]
    return {
        "disclosure_id": disclosure_id,
        "language": language,
        "body": body,
        "body_sha256": content_sha256(body),
        "is_dummy": True,
    }


def screening_handler(request: httpx.Request) -> httpx.Response:
    path, params = request.url.path, request.url.params
    if path == "/v1/ranking/rank":
        return httpx.Response(200, json=ranking_json(json.loads(request.content)))
    if path == "/v1/quotes/alternatives":
        return httpx.Response(200, json=alternatives_json(json.loads(request.content)))
    if path.startswith("/v1/disclosures/sets/"):
        uin = path.rsplit("/", 1)[1]
        found = seed_set(uin, params.get("language", "en-IN"), params.get("channel", "web"))
        return httpx.Response(200, json=found.model_dump(mode="json"))
    if path == "/v1/disclosures/DISC-GLOBAL-TAX-05":
        return httpx.Response(200, json=disclosure_json("DISC-GLOBAL-TAX-05", params["language"]))
    if path == "/v1/suitability/required-slots":
        return httpx.Response(200, json=REQUIRED_SLOTS)
    if path == "/v1/suitability/evaluate":
        return httpx.Response(200, json=suitability_json(json.loads(request.content)))
    if path == "/v1/eligibility/required-attributes":
        return httpx.Response(200, json=REQUIRED_ATTRIBUTES)
    if path.startswith("/v1/reference/pincodes/"):
        pincode = path.rsplit("/", 1)[1]
        if pincode not in PINCODES:
            return problem(404, "NOT_FOUND")
        district, serviceable = PINCODES[pincode]
        info = {"pincode": pincode, "district": district, "state": "Maharashtra"}
        return httpx.Response(200, json={**info, "serviceable": serviceable, "is_dummy": True})
    if path == "/v1/reference/occupations":
        q = params.get("q", "").casefold()
        found = [o for o in OCCUPATIONS if q in f"{o['code']} {o['label']}".casefold()]
        return httpx.Response(200, json=[{**o, "is_dummy": True} for o in found])
    if path.startswith("/v1/reference/occupations/"):
        code = path.rsplit("/", 1)[1]
        found = [{**o, "is_dummy": True} for o in OCCUPATIONS if o["code"] == code]
        return httpx.Response(200, json=found[0]) if found else problem(404, "NOT_FOUND")
    if path == "/v1/eligibility/evaluate":
        return httpx.Response(200, json=eligibility_json(json.loads(request.content)))
    if path == "/v1/catalog/products":
        return httpx.Response(200, json=[product_json(uin) for uin in PRODUCTS])
    if path.startswith("/v1/catalog/products/"):
        uin = path.rsplit("/", 1)[1]
        return (
            httpx.Response(200, json=product_json(uin))
            if uin in PRODUCTS
            else problem(404, "NOT_FOUND")
        )
    if path == "/v1/quotes":
        return quote_json(json.loads(request.content))
    if path == "/v1/disclosures/DISC-GLOBAL-QUOTE-02":
        body = "DUMMY: Premium is indicative, final after underwriting."
        digest = hashlib.sha256(body.encode()).hexdigest()
        return httpx.Response(
            200,
            json={
                "disclosure_id": "DISC-GLOBAL-QUOTE-02",
                "language": params.get("language"),
                "body": body,
                "body_sha256": digest,
                "is_dummy": True,
            },  # fmt: skip
        )
    raise AssertionError(f"unexpected domain call {request.method}")


def domain(handler: Callable[[httpx.Request], httpx.Response] = domain_handler) -> DomainClient:
    return DomainClient(DOMAIN, "t0ken", transport=httpx.MockTransport(handler))


def required_env(name: str) -> str:
    dsn = os.environ.get(name)
    if not dsn:
        pytest.fail(f"{name} is not set; run `make up && make check-db`")
    return dsn


@asynccontextmanager
async def db_runtime(
    keys: KeyService,
    *,
    models: "Models | None" = None,
    handler: Callable[[httpx.Request], httpx.Response] = domain_handler,
    **config: Any,
) -> AsyncIterator[Runtime]:
    """A real Runtime on the test database: app_rw and erasure_rw pools, the checkpointer on its
    own autocommit pool (as Runtime.open builds it), the model routes and the domain tier behind
    MockTransport, and the in-memory gate. `saver_pool` is exposed for a checkpointer swap."""
    dsn = required_env("SS_TEST_PG_DSN_APP")
    saver_pool: AsyncConnectionPool[Any] = AsyncConnectionPool(
        dsn,
        min_size=1,
        max_size=4,
        open=False,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "prepare_threshold": 0,
            "options": "-c search_path=langgraph",
        },
    )
    await saver_pool.open()
    cfg = settings(**config)
    try:
        with (
            ConnectionPool(dsn, min_size=1, max_size=4) as pool,
            ConnectionPool(required_env("SS_TEST_PG_DSN_ERASURE"), min_size=1) as erasure,
        ):
            async with gateway(models or Models(), cfg) as gw, domain(handler) as dom:
                rt = Runtime(
                    cfg,
                    pool=pool,
                    erasure=erasure,
                    keys=keys,
                    gate=FakeGate(),  # type: ignore[arg-type]
                    domain=dom,
                    gateway=gw,
                    pack=load_pack(cfg.output_lexicon),
                    graph=build_graph(AsyncPostgresSaver(saver_pool)),
                )
                rt.saver_pool = saver_pool  # type: ignore[attr-defined]
                yield rt
    finally:
        await saver_pool.close()


class FakeGate:
    """RedisGate in memory. `down` makes every call fail as a Redis outage does."""

    def __init__(self, *, limited: bool = False, down: bool = False) -> None:
        self.limited, self.down = limited, down
        self.locks: dict[UUID, str] = {}
        self.idem: dict[UUID, str] = {}
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.released: list[UUID] = []

    def _check(self) -> None:
        if self.down:
            raise RedisConnectionError("valkey down")

    async def acquire(self, session_id: UUID) -> str | None:
        self._check()
        if session_id in self.locks:
            return None
        self.locks[session_id] = "token"
        return "token"

    async def release(self, session_id: UUID, token: str) -> None:
        self._check()
        if self.locks.get(session_id) == token:
            del self.locks[session_id]
            self.released.append(session_id)

    async def over_limit(self, subject_ref: UUID) -> bool:
        self._check()
        return self.limited

    async def get_idem(self, turn_key: UUID) -> str | None:
        self._check()
        return self.idem.get(turn_key)

    async def set_idem(self, turn_key: UUID, rendered_sha256: str) -> None:
        self._check()
        self.idem[turn_key] = rendered_sha256

    async def publish(self, session_id: UUID, event: str, data: dict[str, Any]) -> None:
        self._check()
        self.published.append((event, data))

    async def subscribe(self, session_id: UUID) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """What was published so far, then the stream ends (Redis's never does)."""
        self._check()
        for event, data in list(self.published):
            yield event, data
