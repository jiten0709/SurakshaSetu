"""Async client for the domain tier. Every shape comes from models.py, generated from the contract.

Nothing here retries. A consent write may be retried only by calling create_consent_record again
with the same idempotency key, which the domain tier answers with the original record.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from surakshasetu.config import Settings
from surakshasetu.domain.models import (
    ConsentNotice,
    ConsentRecord,
    ConsentRecordCreate,
    ConsentWithdrawal,
    Disclosure,
    DisclosureSet,
    EligibilityRequest,
    EligibilityResult,
    KillSwitch,
    KillSwitchResult,
    Occupation,
    PincodeInfo,
    PremiumQuote,
    Problem,
    Product,
    ProductStatus,
    ProductType,
    PurposeGrant,
    QuoteAlternative,
    QuoteAlternativesRequest,
    QuoteRequest,
    RankingRequest,
    RankingResult,
    RequiredAttribute,
    RequiredSlot,
    Rider,
    SuitabilityRequest,
    SuitabilityResult,
    Versions,
)

logger = logging.getLogger(__name__)

# Whole-call budgets: rule evaluations get 300 ms; keyed lookups and consent writes get 150 ms.
DECISION_BUDGET_S = 0.300
LOOKUP_BUDGET_S = 0.150

_PRODUCTS = TypeAdapter(list[Product])
_OCCUPATIONS = TypeAdapter(list[Occupation])
_REQUIRED_ATTRIBUTES = TypeAdapter(list[RequiredAttribute])
_REQUIRED_SLOTS = TypeAdapter(list[RequiredSlot])
_ALTERNATIVES = TypeAdapter(list[QuoteAlternative])


class DomainError(Exception):
    """A domain-tier failure. status is None when no response arrived (TIMEOUT, UNAVAILABLE)."""

    def __init__(self, code: str, status: int | None, problem: Problem | None = None) -> None:
        super().__init__(f"{code} ({status})" if status else code)
        self.code = code
        self.status = status
        self.problem = problem


def _error(response: httpx.Response) -> DomainError:
    try:
        problem = Problem.model_validate_json(response.content)
    except ValidationError:
        return DomainError("UNEXPECTED_RESPONSE", response.status_code)
    return DomainError(problem.code, response.status_code, problem)


def _iso(as_of: datetime | None) -> str | None:
    return None if as_of is None else as_of.isoformat()


class DomainClient:
    def __init__(
        self, base_url: str, token: str, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={"Authorization": f"Bearer {token}"},
            transport=transport,
            timeout=DECISION_BUDGET_S,  # backstop; each call's budget is the real limit
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(settings.domain_base_url, settings.domain_token.get_secret_value())

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def _call[T](
        self,
        op: str,
        parse: Callable[[bytes], T],
        method: str,
        path: str,
        *,
        budget_s: float = LOOKUP_BUDGET_S,
        body: BaseModel | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> T:
        # Unset optional fields are omitted and explicit nulls kept; inputs_sha256 covers exactly
        # this JSON.
        payload = None if body is None else body.model_dump(mode="json", exclude_unset=True)
        query = None if params is None else {k: v for k, v in params.items() if v is not None}
        # Logs name the call by its contract operationId only: paths and queries carry pincodes,
        # consent ids and customer text, and str(exc) of an httpx error embeds the URL.
        started = time.perf_counter()
        try:
            async with asyncio.timeout(budget_s):
                response = await self._http.request(
                    method, path, json=payload, params=query, headers=headers
                )
        except TimeoutError as exc:
            logger.warning("domain %s timed out after %d ms", op, budget_s * 1000)
            raise DomainError("TIMEOUT", None) from exc
        except httpx.TransportError as exc:
            logger.warning("domain %s unavailable (%s)", op, type(exc).__name__)
            raise DomainError("UNAVAILABLE", None) from exc
        elapsed_ms = (time.perf_counter() - started) * 1000
        if response.is_error:
            error = _error(response)
            logger.log(
                logging.WARNING if response.status_code >= 500 else logging.INFO,
                "domain %s -> %d %s in %.0f ms",
                op,
                response.status_code,
                error.code,
                elapsed_ms,
            )
            raise error
        logger.debug("domain %s -> %d in %.0f ms", op, response.status_code, elapsed_ms)
        return parse(response.content)

    # --- meta -------------------------------------------------------------------------------
    async def get_versions(self) -> Versions:
        return await self._call(
            "getVersions", Versions.model_validate_json, "GET", "/v1/meta/versions"
        )

    # --- consent ----------------------------------------------------------------------------
    async def get_current_consent_notice(self, language: str) -> ConsentNotice:
        return await self._call(
            "getCurrentConsentNotice",
            ConsentNotice.model_validate_json,
            "GET",
            "/v1/consent/notices/current",
            params={"language": language},
        )

    async def get_consent_notice(self, notice_version: str) -> ConsentNotice:
        return await self._call(
            "getConsentNotice",
            ConsentNotice.model_validate_json,
            "GET",
            f"/v1/consent/notices/{notice_version}",
        )

    async def create_consent_record(
        self, record: ConsentRecordCreate, idempotency_key: str
    ) -> ConsentRecord:
        return await self._call(
            "createConsentRecord",
            ConsentRecord.model_validate_json,
            "POST",
            "/v1/consent/records",
            body=record,
            headers={"Idempotency-Key": idempotency_key},
        )

    async def get_consent_record(
        self, consent_id: UUID, as_of: datetime | None = None
    ) -> ConsentRecord:
        return await self._call(
            "getConsentRecord",
            ConsentRecord.model_validate_json,
            "GET",
            f"/v1/consent/records/{consent_id}",
            params={"as_of": _iso(as_of)},
        )

    async def change_consent_purpose(self, consent_id: UUID, grant: PurposeGrant) -> ConsentRecord:
        return await self._call(
            "changeConsentPurpose",
            ConsentRecord.model_validate_json,
            "POST",
            f"/v1/consent/records/{consent_id}/purposes",
            body=grant,
        )

    async def withdraw_consent(
        self, consent_id: UUID, withdrawal: ConsentWithdrawal
    ) -> ConsentRecord:
        return await self._call(
            "withdrawConsent",
            ConsentRecord.model_validate_json,
            "POST",
            f"/v1/consent/records/{consent_id}/withdraw",
            body=withdrawal,
        )

    # --- catalog ----------------------------------------------------------------------------
    async def list_products(
        self,
        status: ProductStatus | None = None,
        category: ProductType | None = None,
        launch_enabled: bool | None = None,
        as_of: datetime | None = None,
    ) -> list[Product]:
        return await self._call(
            "listProducts",
            _PRODUCTS.validate_json,
            "GET",
            "/v1/catalog/products",
            params={
                "status": status,
                "category": category,
                "launch_enabled": launch_enabled,
                "as_of": _iso(as_of),
            },
        )

    async def get_product(self, uin: str, as_of: datetime | None = None) -> Product:
        return await self._call(
            "getProduct",
            Product.model_validate_json,
            "GET",
            f"/v1/catalog/products/{uin}",
            params={"as_of": _iso(as_of)},
        )

    async def get_rider(self, uin: str) -> Rider:
        return await self._call(
            "getRider", Rider.model_validate_json, "GET", f"/v1/catalog/riders/{uin}"
        )

    async def set_product_kill_switch(self, uin: str, kill_switch: KillSwitch) -> KillSwitchResult:
        return await self._call(
            "setProductKillSwitch",
            KillSwitchResult.model_validate_json,
            "POST",
            f"/v1/catalog/products/{uin}/kill-switch",
            body=kill_switch,
        )

    # --- disclosure -------------------------------------------------------------------------
    async def get_disclosure_set(
        self, uin: str, channel: str, language: str, as_of: datetime | None = None
    ) -> DisclosureSet:
        return await self._call(
            "getDisclosureSet",
            DisclosureSet.model_validate_json,
            "GET",
            f"/v1/disclosures/sets/{uin}",
            params={"channel": channel, "language": language, "as_of": _iso(as_of)},
        )

    async def get_disclosure(
        self, disclosure_id: str, language: str, as_of: datetime | None = None
    ) -> Disclosure:
        return await self._call(
            "getDisclosure",
            Disclosure.model_validate_json,
            "GET",
            f"/v1/disclosures/{disclosure_id}",
            params={"language": language, "as_of": _iso(as_of)},
        )

    # --- reference --------------------------------------------------------------------------
    async def get_pincode(self, pincode: str) -> PincodeInfo:
        return await self._call(
            "getPincode",
            PincodeInfo.model_validate_json,
            "GET",
            f"/v1/reference/pincodes/{pincode}",
        )

    async def search_occupations(self, q: str | None = None) -> list[Occupation]:
        return await self._call(
            "searchOccupations",
            _OCCUPATIONS.validate_json,
            "GET",
            "/v1/reference/occupations",
            params={"q": q},
        )

    async def get_occupation(self, code: str) -> Occupation:
        return await self._call(
            "getOccupation",
            Occupation.model_validate_json,
            "GET",
            f"/v1/reference/occupations/{code}",
        )

    # --- decisions --------------------------------------------------------------------------
    async def get_required_attributes(
        self, rules: str, as_of: datetime | None = None
    ) -> list[RequiredAttribute]:
        return await self._call(
            "getRequiredAttributes",
            _REQUIRED_ATTRIBUTES.validate_json,
            "GET",
            "/v1/eligibility/required-attributes",
            params={"pins.rules": rules, "as_of": _iso(as_of)},
        )

    async def evaluate_eligibility(self, request: EligibilityRequest) -> EligibilityResult:
        return await self._call(
            "evaluateEligibility",
            EligibilityResult.model_validate_json,
            "POST",
            "/v1/eligibility/evaluate",
            body=request,
            budget_s=DECISION_BUDGET_S,
        )

    async def get_required_slots(self, rules: str) -> list[RequiredSlot]:
        return await self._call(
            "getRequiredSlots",
            _REQUIRED_SLOTS.validate_json,
            "GET",
            "/v1/suitability/required-slots",
            params={"pins.rules": rules},
        )

    async def evaluate_suitability(self, request: SuitabilityRequest) -> SuitabilityResult:
        return await self._call(
            "evaluateSuitability",
            SuitabilityResult.model_validate_json,
            "POST",
            "/v1/suitability/evaluate",
            body=request,
            budget_s=DECISION_BUDGET_S,
        )

    async def rank_options(self, request: RankingRequest) -> RankingResult:
        return await self._call(
            "rankOptions",
            RankingResult.model_validate_json,
            "POST",
            "/v1/ranking/rank",
            body=request,
            budget_s=DECISION_BUDGET_S,
        )

    async def create_quote(self, request: QuoteRequest) -> PremiumQuote:
        return await self._call(
            "createQuote",
            PremiumQuote.model_validate_json,
            "POST",
            "/v1/quotes",
            body=request,
            budget_s=DECISION_BUDGET_S,
        )

    async def create_quote_alternatives(
        self, request: QuoteAlternativesRequest
    ) -> list[QuoteAlternative]:
        return await self._call(
            "createQuoteAlternatives",
            _ALTERNATIVES.validate_json,
            "POST",
            "/v1/quotes/alternatives",
            body=request,
            budget_s=DECISION_BUDGET_S,
        )
