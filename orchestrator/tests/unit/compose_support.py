"""Shared by the compose unit tests: the real prompt bundle, plus engine results, catalog rows,
registry sets and evidence shaped like the DUMMY seed."""

from datetime import date
from functools import cache
from typing import Literal
from uuid import UUID

from surakshasetu.compose.bundle import PromptBundle, load_bundle
from surakshasetu.domain.models import (
    DisclosureItem,
    DisclosureSet,
    NeedsPayload,
    PremiumQuote,
    Product,
    ProductDocument,
    QuoteDefaults,
    RankingResult,
    RecommendedOption,
    Rider,
    SuitabilityAssumptions,
    SuitabilityResult,
)
from surakshasetu.kb.payload import Collection
from surakshasetu.retrieval.service import EvidenceChunk, RetrievalAudit, RetrievalResult

TERM, ROP = "999N001V02", "999N002V01"
SHA = "ab" * 32
DECISION = UUID("0190a0c4-0000-7000-8000-000000000001")


@cache
def bundle() -> PromptBundle:
    return load_bundle("pb-2026.10.8", env="test")


def quote(
    uin: str, premium: str = "12345.00", ppt: Literal["regular", "single"] = "regular"
) -> PremiumQuote:
    return PremiumQuote(
        decision_id=DECISION,
        quote_id=f"Q-2026-10-01-{uin[-4:]}",
        uin=uin,
        sum_assured_inr="10000000",
        term_years=30,
        ppt=ppt,
        annual_premium_inr=premium,
        frequency="single" if ppt == "single" else "annual",
        valid_until=date(2026, 10, 31),
        indicative=True,
        rider_premiums={},
        gst_included=True,
        rating_version="rating-dummy-2026.09.1",
        inputs_sha256=SHA,
        reason_codes=[],
    )


def option(
    rank: int,
    uin: str,
    *,
    cover: str = "10000000",
    priced: bool = True,
    gap: str = "0",
    riders: list[str] | None = None,
) -> RecommendedOption:
    return RecommendedOption(
        rank=rank,
        uin=uin,
        sum_assured_inr=cover,
        term_years=30,
        ppt_years=30,
        rider_uins=riders or [],
        quote=quote(uin) if priced else None,
        reason_codes=["RANK-FIT-TERM"],
        protection_gap_inr=gap,
    )


def ranking(*options: RecommendedOption) -> RankingResult:
    return RankingResult(
        decision_id=DECISION,
        options=list(options),
        ranker_version="ranker-2026.09.1",
        suitability_inputs_sha256=SHA,
        inputs_sha256=SHA,
        reason_codes=[],
    )


def suitability(need: str = "12500000.00") -> SuitabilityResult:
    return SuitabilityResult(
        decision_id=DECISION,
        outcome="FIT",
        profile_sufficiency=1.0,
        fit_types=["TERM", "TERM_ROP"],
        excluded={},
        need_inr=need,
        recommended_cover_inr="10000000",
        uw_cap_inr="20000000",
        term_years=30,
        affordability="green",
        vulnerability_flags=[],
        assumptions=SuitabilityAssumptions(
            cover_to_age=60,
            dependency_years=21,
            discount_rate="0.07",
            income_growth="0.065",
            consumption_share="0.3",
            final_expenses_inr="200000",
            existing_cover_counted_inr="500000",
        ),
        rule_ids=["FIT-01", "AFF-01"],
        reason_codes=[],
        params_version="actuarial-2026.09.1",
        rules_version="2026.09.1",
        inputs_sha256=SHA,
    )


def needs() -> NeedsPayload:
    return NeedsPayload(
        goals=["income_protection", "loan_cover"],
        annual_income_inr="1200000",
        income_type="salaried",
        existing_annual_premium_inr="0",
        financial_distress=False,
        comprehension_difficulty_count=0,
    )


def document(
    uin: str, kind: Literal["CIS", "BI", "POLICY_WORDING"], version: str, language: str = "en-IN"
) -> ProductDocument:
    return ProductDocument(
        kind=kind,
        version=version,
        language=language,
        uri=f"content/seed/kb/product/{uin}-{kind.lower()}-{version}.md",
        sha256=f"{kind}{version}{language}".encode().hex().ljust(64, "0")[:64],
    )


def product(uin: str) -> Product:
    rop = uin == ROP
    return Product(
        uin=uin,
        name="Suraksha Term Shield ROP" if rop else "Suraksha Term Shield",
        category="TERM_ROP" if rop else "TERM",
        status="in_force",
        entry_age_min=18,
        entry_age_max=55 if rop else 65,
        maturity_age_max=75 if rop else 85,
        sa_min_inr="2500000.00",
        sa_max_inr="20000000.00" if rop else "100000000.00",
        term_years_min=15 if rop else 10,
        term_years_max=35 if rop else 40,
        ppt_options=["regular", "limited_10"] if rop else ["regular", "limited_10", "single"],
        benefit_payment_options=["lumpsum"] if rop else ["lumpsum", "monthly_income"],
        rider_uins=["999A007V01"],
        effective_from=date(2026, 9, 1),
        effective_to=None,
        launch_enabled=True,
        is_dummy=True,
        riders=[
            Rider(
                uin="999A007V01",
                name="Accidental death benefit",
                attaches_to=[TERM, ROP],
                sa_max_inr=None,
                is_dummy=True,
            )
        ],
        documents=[
            document(uin, "CIS", "v1"),
            document(uin, "CIS", "v2"),
            document(uin, "POLICY_WORDING", "v10"),
            document(uin, "POLICY_WORDING", "v2"),
        ],
        quote_defaults=QuoteDefaults(
            sum_assured_inr="5000000" if rop else "10000000",
            term_years=25 if rop else 30,
            ppt="regular",
            frequency="annual",
            rider_uins=[],
        ),
    )


def disclosure_set(uin: str, language: str = "en-IN") -> DisclosureSet:
    bodies = [
        "DUMMY: Approved solicitation statement.",
        "DUMMY: Premium is indicative, final after underwriting.",
        f"DUMMY: Key exclusions and waiting periods summary for {uin}.",
    ]
    return DisclosureSet(
        uin=uin,
        channel="web",
        language=language,
        registry_version="2026.09.1",
        items=[
            DisclosureItem(disclosure_id=f"DISC-{i}", body=body, body_sha256=SHA)
            for i, body in enumerate(bodies)
        ],
        set_sha256=(uin.encode().hex() * 4)[:64],
        is_dummy=True,
    )


def chunk(
    handle: str,
    *,
    domain: Collection = "product",
    score: float | None = 0.9,
    parent: bool = False,
    text: str = "DUMMY: The policy pays the death benefit to the nominee.",
) -> EvidenceChunk:
    return EvidenceChunk(
        handle=handle,
        chunk_id=f"{domain}:doc:{handle.lower()}:abc123",
        citation_label=f"SurakshaTermShield_999N001V02_PolicyWording §{handle[1:]}",
        text=text,
        domain=domain,
        rerank_score=score,
        content_sha256=SHA,
        section_path=["Suraksha Term Shield (999N001V02)", "Policy Wording v2", f"{handle[1:]}. S"],
        source_uri="content/seed/kb/product/999N001V02-policy-wording-v2.md",
        effective_from=date(2026, 9, 1),
        doc_title="Suraksha Term Shield: Policy Wording v2",
        version="v2",
        doc_type="policy_wording",
        precedence=2,
        parent=parent,
    )


def retrieved(chunks: list[EvidenceChunk], quotas: dict[Collection, int]) -> RetrievalResult:
    return RetrievalResult(
        evidence=chunks,
        abstained=False,
        abstain_reason=None,
        audit=RetrievalAudit(
            rewritten_query="q",
            lexical_query="q",
            route_rule="RT-DEFAULT",
            collections=sorted({c.domain for c in chunks}),
            snapshot_ids=[],
            candidates={},
            chunk_ids=[c.chunk_id for c in chunks],
            content_sha256=[c.content_sha256 for c in chunks],
            rerank_scores=[c.rerank_score for c in chunks],
            scoring="rerank",
            handle_map={c.handle: c.chunk_id for c in chunks},
            degraded=False,
            abstained=False,
            abstain_reason=None,
        ),
        quotas=quotas,
    )
