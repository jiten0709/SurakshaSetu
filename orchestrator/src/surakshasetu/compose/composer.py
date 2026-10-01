"""The S3 composer (TDD §3.8): the recommendation turn, assembled in a fixed order from templates,
engine values and registry text, with the model's narrative as the one generated part:

    needs recap, option cards, comparison, why it fits, disclosures, call to action (then sources)

Every number and table cell comes from the engine results or the Product Catalog; the narrative's
numbers come only through placeholders, and its citations only through this turn's handles.
Disclosure bodies are inserted verbatim from the registry response, never from a template or a
model. Without a narrative (a rail failed twice, or the route is down) the same parts frame the
deterministic card instead. rendered_sha256 is the SHA-256 of exactly the text released.
"""

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from surakshasetu.compose.bundle import Attribute, Cta, Labels, PromptBundle, Recommendation
from surakshasetu.compose.citations import Cited, Source, TurnHandles, render, source_list
from surakshasetu.compose.placeholders import fill, format_date, format_inr
from surakshasetu.domain.models import (
    DisclosureSet,
    NeedsPayload,
    Product,
    ProductDocument,
    RankingResult,
    RecommendedOption,
    SuitabilityResult,
)

logger = logging.getLogger(__name__)

SHOWN_KINDS = ("CIS", "BI", "POLICY_WORDING")  # BI only where the product has one (savings)


class CompositionError(Exception):
    """The turn cannot be composed; the caller falls back to a template and an advisor offer.
    reason: LOCALE_UNKNOWN, NO_OPTIONS, PRODUCT_MISSING, DISCLOSURES_MISSING,
    DISCLOSURES_MISMATCH, DOCUMENT_MISSING or TEMPLATE_INVALID."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Rendered:
    text: str
    rendered_sha256: str  # SHA-256(UTF-8(text))
    parts: list[tuple[str, str]]  # (part id, text), in order
    citations: dict[str, str]  # each handle cited -> chunk_id or rule id
    sources: list[Source]
    disclosure_hashes: dict[str, str]  # UIN -> set_sha256 of the set shown
    documents_shown: dict[str, dict[str, str]]  # UIN -> {CIS, BI?, POLICY_WORDING: sha256}


def compose(
    bundle: PromptBundle,
    *,
    locale: str,
    needs: NeedsPayload,
    suitability: SuitabilityResult,
    ranking: RankingResult,
    products: Mapping[str, Product],
    disclosure_sets: Mapping[str, DisclosureSet],
    handles: TurnHandles,
    narrative: str | None,  # the validated gen-recommend text; None: the deterministic card
    partial_profile: bool = False,  # S3 after an election below the sufficiency threshold
) -> Rendered:
    templates = bundle.templates.get(locale)
    if templates is None:
        raise _error("LOCALE_UNKNOWN")
    options = ranking.options
    if not options:
        raise _error("NO_OPTIONS")
    if any(o.uin not in products for o in options):
        raise _error("PRODUCT_MISSING")
    if any(o.uin not in disclosure_sets for o in options):
        raise _error("DISCLOSURES_MISSING")
    rec, labels = templates.recommendation, templates.scripts.labels
    shown = {o.uin: _documents(products[o.uin], locale) for o in options}

    placeholders = 0
    if narrative is None:
        cited = Cited("\n".join([rec.deterministic_card, templates.scripts.advisor_offer]), [], [])
    else:
        filled, placeholders = fill(narrative, ranking, suitability)
        cited = render(filled, handles)
    try:
        parts = [("needs_recap", _needs_recap(rec, labels, needs, suitability, partial_profile))]
        parts += [
            (f"option_card:{o.uin}", _card(rec, labels, o, products[o.uin], shown[o.uin]))
            for o in options
        ]
        if len(options) > 1:
            parts.append(
                ("comparison", _comparison(rec, labels, [products[o.uin] for o in options]))
            )
        parts.append(("why_it_fits", cited.text))
        parts += [
            (
                f"disclosures:{o.uin}",
                _disclosures(rec, products[o.uin], disclosure_sets[o.uin], locale),
            )
            for o in options
        ]
        parts.append(("cta", _cta(rec.cta)))
        if cited.sources:
            parts.append(("sources", source_list(cited.sources, rec)))
    except (KeyError, ValueError) as exc:  # a template field or label the bundle doesn't define
        raise _error("TEMPLATE_INVALID") from exc

    text = "\n\n".join(part for _, part in parts)
    logger.info(
        "composed S3 %s: %d options, %d placeholders, %d citations",
        "deterministic card" if narrative is None else "cited generation",
        len(options),
        placeholders,
        len(cited.handles),
    )
    return Rendered(
        text=text,
        rendered_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        parts=parts,
        citations=handles.evidence_map(cited.handles),
        sources=cited.sources,
        disclosure_hashes={o.uin: disclosure_sets[o.uin].set_sha256 for o in options},
        documents_shown={uin: {d.kind: d.sha256 for d in docs} for uin, docs in shown.items()},
    )


def _needs_recap(
    rec: Recommendation,
    labels: Labels,
    needs: NeedsPayload,
    suitability: SuitabilityResult,
    partial_profile: bool,
) -> str:
    a = suitability.assumptions
    recap = rec.needs_recap.format(
        goals=", ".join(labels.goals[g] for g in needs.goals) or labels.none,
        need=format_inr(suitability.need_inr),
        cover=format_inr(suitability.recommended_cover_inr),
        term=suitability.term_years,
        cover_to_age=a.cover_to_age,
        dependency_years=a.dependency_years,
        existing_cover=format_inr(a.existing_cover_counted_inr),
        growth=_percent(a.income_growth),
        discount=_percent(a.discount_rate),
    )
    return f"{recap}\n{rec.partial_profile}" if partial_profile else recap


def _card(
    rec: Recommendation,
    labels: Labels,
    option: RecommendedOption,
    product: Product,
    documents: list[ProductDocument],
) -> str:
    quote = option.quote
    if quote is None:
        premium = rec.premium_withheld
    else:
        premium = (rec.premium_single if quote.ppt == "single" else rec.premium).format(
            amount=format_inr(quote.annual_premium_inr),
            quote_id=quote.quote_id,
            valid_until=format_date(quote.valid_until),
        )
    riders = {r.uin: r.name for r in product.riders}
    return rec.option_card.format(
        rank=option.rank,
        name=product.name,
        uin=option.uin,
        cover=format_inr(option.sum_assured_inr),
        term=option.term_years,
        ppt=option.ppt_years,
        riders=", ".join(riders.get(u, u) for u in option.rider_uins) or labels.none,
        premium=premium,
        gap=format_inr(option.protection_gap_inr),
        documents="; ".join(
            rec.document.format(label=labels.documents[d.kind], version=d.version, uri=d.uri)
            for d in documents
        ),
    )


def _comparison(rec: Recommendation, labels: Labels, products: list[Product]) -> str:
    """The fixed attribute set of each option's category, in rank order, from the catalog."""
    table = rec.comparison
    keys = list(dict.fromkeys(k for p in products for k in table.attributes[p.category]))
    rows = [
        "| | " + " | ".join(f"{p.name} ({p.uin})" for p in products) + " |",
        "|---" * (len(products) + 1) + "|",
        *(
            f"| {table.rows[k]} | " + " | ".join(_attribute(k, p, labels) for p in products) + " |"
            for k in keys
        ),
    ]
    return "\n".join([table.heading, *rows])


def _attribute(key: Attribute, p: Product, labels: Labels) -> str:
    match key:
        case "category":
            return labels.product_types[p.category]
        case "entry_age":
            return f"{p.entry_age_min}–{p.entry_age_max}"
        case "maturity_age":
            return str(p.maturity_age_max)
        case "cover_range":
            low = format_inr(p.sa_min_inr)
            if p.sa_max_inr is None:
                return labels.or_more.format(amount=low)
            return f"{low}–{format_inr(p.sa_max_inr)}"
        case "term_range":
            return f"{p.term_years_min}–{p.term_years_max}"
        case "ppt_options":
            return ", ".join(labels.ppt[x] for x in p.ppt_options)
        case "benefit_payment":
            return ", ".join(labels.benefit_payment[x] for x in p.benefit_payment_options)
        case "riders":
            return ", ".join(r.name for r in p.riders) or labels.none


def _disclosures(rec: Recommendation, product: Product, shown: DisclosureSet, locale: str) -> str:
    """The registry's set for this UIN and language, every body verbatim and in order."""
    if shown.uin != product.uin or shown.language != locale:
        raise _error("DISCLOSURES_MISMATCH")
    heading = rec.disclosures_heading.format(name=product.name, uin=product.uin)
    return "\n".join([heading, *(item.body for item in shown.items)])


def _cta(cta: Cta) -> str:
    return "\n".join(
        [cta.heading, *(f"- {c}" for c in (cta.apply, cta.advisor, cta.revise, cta.save))]
    )


def _documents(product: Product, locale: str) -> list[ProductDocument]:
    """The newest version of each document kind in the locale, else in en-IN (the seed has en-IN
    documents only). CIS and the policy wording are mandatory at the point of sale."""

    def newest(language: str) -> dict[str, ProductDocument]:
        found = sorted(
            (d for d in product.documents if d.language == language),
            key=lambda d: _natural(d.version),
        )
        return {d.kind: d for d in found}

    chosen = {**newest("en-IN"), **newest(locale)}
    if "CIS" not in chosen or "POLICY_WORDING" not in chosen:
        raise _error("DOCUMENT_MISSING")
    return [chosen[kind] for kind in SHOWN_KINDS if kind in chosen]


def _natural(version: str) -> list[tuple[int, int | str]]:
    """v10 after v2."""
    return [(0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"(\d+)", version) if p]


def _percent(rate: str) -> str:
    return f"{(Decimal(rate) * 100).normalize():f}%"


def _error(reason: str) -> CompositionError:
    logger.warning("S3 composition failed: %s", reason)
    return CompositionError(reason)
