"""The model path. Application code names a Route and a data class, never a model, provider or URL.

Chat routes go to the model gateway (OmniRoute) over the OpenAI-compatible HTTP API. embed and
rerank go straight to the self-hosted TEI services: the gateway hosts
no embedding or rerank models, and query text must stay on the self-hosted network.

Nothing here retries. Every failure raises GatewayUnavailable, so callers fall back to a template
or to BM25-only retrieval: the model path fails closed.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Any, Literal, NamedTuple, Never, Self, overload
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex

logger = logging.getLogger(__name__)


class Route(StrEnum):
    """The combos of TDD §1.5. A chat route's name is what the gateway receives as `model`."""

    GUARD_INPUT = "guard-input"
    NLU_EXTRACT = "nlu-extract"
    GEN_CONVERSE = "gen-converse"
    GEN_RECOMMEND = "gen-recommend"
    VERIFY_CLAIMS = "verify-claims"
    SUMMARISE = "summarise"
    EMBED = "embed"
    RERANK = "rerank"


class DataClass(StrEnum):
    SELF_HOSTED_RAW = "self_hosted_raw"
    REDACTED = "redacted"


class RouteSpec(NamedTuple):
    timeout_s: float
    self_hosted: bool  # the route's models run inside the insurer boundary, so raw text may go


# TDD §1.5: the whole-call timeout and data rule of each route.
ROUTES: dict[Route, RouteSpec] = {
    Route.GUARD_INPUT: RouteSpec(0.200, self_hosted=True),
    Route.NLU_EXTRACT: RouteSpec(0.800, self_hosted=True),
    Route.GEN_CONVERSE: RouteSpec(2.0, self_hosted=False),
    Route.GEN_RECOMMEND: RouteSpec(6.0, self_hosted=False),
    Route.VERIFY_CLAIMS: RouteSpec(1.0, self_hosted=True),
    Route.SUMMARISE: RouteSpec(1.0, self_hosted=True),
    Route.EMBED: RouteSpec(0.150, self_hosted=True),
    Route.RERANK: RouteSpec(0.300, self_hosted=True),
}

# OmniRoute sets this on every chat response: "<mode>; source=<source>", plus "; tokens=a->b;
# rules: …" segments when rules rewrote the prompt (3.8.50; "off; source=off" when disabled).
# Compression must never fire (TDD §1.6): anything but an off mode with no rewrite counts as fired.
COMPRESSION_HEADER = "x-omniroute-compression"
_COMPRESSION_OFF = frozenset({"off", "none", "false", "0"})
_COMPRESSION_REWRITE = ("tokens=", "rules:")


@dataclass(frozen=True)
class RedactionAttestation:
    """Issued by the envelope builder (Step 13) once a PII scan of exactly these messages is clean:
    envelope_sha256 = SHA-256(JCS(messages))."""

    envelope_sha256: str


@dataclass(frozen=True)
class GatewayResult[T: BaseModel]:
    content: str
    parsed: T | None  # set when the call passed a response_format
    served_model: str  # the model that answered, read from the response, never the route
    tokens_in: int
    tokens_out: int
    latency_ms: float
    fallback_hops: int = 0  # OmniRoute 3.8.50 reports no fallback count on chat responses


@dataclass(frozen=True)
class TeiModel:
    """What a TEI service serves (its GET /info). A corpus snapshot is stamped with the embedding
    model at ingestion, and the retrieval thresholds with the reranker at calibration, so retrieval
    can refuse either when another model is served."""

    model_id: str
    model_sha: str


class GatewayUnavailable(Exception):
    """No usable model answer; the caller takes its template path. status is the HTTP status when
    a response arrived."""

    def __init__(self, reason: str, status: int | None = None) -> None:
        super().__init__(f"{reason} ({status})" if status else reason)
        self.reason = reason
        self.status = status


class GatewayPolicyViolation(GatewayUnavailable):
    """A data-class rule refused the call, or the response broke the gateway hardening profile."""


class _Message(BaseModel):
    content: str


class _Choice(BaseModel):
    message: _Message


class _Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class _Completion(BaseModel):
    model: str
    choices: list[_Choice] = Field(min_length=1)
    usage: _Usage = _Usage()


class _Embedding(BaseModel):
    index: int
    embedding: list[float]


class _Embeddings(BaseModel):
    model: str
    data: list[_Embedding]


class _Hit(BaseModel):
    index: int
    score: float


_HITS = TypeAdapter(list[_Hit])


class _Info(BaseModel):  # TEI's GET /info; the other keys are ignored
    model_id: str
    model_sha: str


class Gateway:
    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._chat_url = f"{settings.gateway_base_url.rstrip('/')}/chat/completions"
        self._embed_url = f"{settings.tei_embed_url.rstrip('/')}/v1/embeddings"
        self._embed_info_url = f"{settings.tei_embed_url.rstrip('/')}/info"
        self._rerank_url = f"{settings.tei_rerank_url.rstrip('/')}/rerank"
        self._rerank_info_url = f"{settings.tei_rerank_url.rstrip('/')}/info"
        # The gateway's own liveness, beside its OpenAI-compatible /v1 (unauthenticated).
        self._health_url = f"{settings.gateway_base_url.rstrip('/').removesuffix('/v1')}/healthz"
        self._api_key = settings.gateway_api_key
        self._embed_dim = settings.embed_dim
        self._query_prefix = settings.embed_query_prefix
        # Dev and test only (pilot and prod refuse anything but 1): CPU TEI misses the GPU budgets.
        self._tei_scale = settings.tei_timeout_scale
        # Backstop only; each call's route timeout is the real limit.
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=max(self._timeout(route) for route in ROUTES),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    # Without a response_format there is nothing to parse: GatewayResult[Never].parsed is None.
    @overload
    async def call(
        self,
        route: Route,
        *,
        data_class: DataClass,
        messages: list[dict[str, str]],
        session_id: UUID,
        turn_id: UUID,
        fsm_state: str,
        response_format: None = None,
        attestation: RedactionAttestation | None = None,
    ) -> GatewayResult[Never]: ...

    @overload
    async def call[T: BaseModel](
        self,
        route: Route,
        *,
        data_class: DataClass,
        messages: list[dict[str, str]],
        session_id: UUID,
        turn_id: UUID,
        fsm_state: str,
        response_format: type[T],
        attestation: RedactionAttestation | None = None,
    ) -> GatewayResult[T]: ...

    async def call(
        self,
        route: Route,
        *,
        data_class: DataClass,
        messages: list[dict[str, str]],
        session_id: UUID,
        turn_id: UUID,
        fsm_state: str,
        response_format: type[BaseModel] | None = None,
        attestation: RedactionAttestation | None = None,
    ) -> GatewayResult[Any]:
        _check_data_class(route, data_class, messages, attestation)
        body: dict[str, Any] = {"model": route.value, "messages": messages, "user": str(session_id)}
        if response_format is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_format.__name__,
                    "schema": response_format.model_json_schema(),
                },
            }
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "X-SS-Turn-Id": str(turn_id),
            "X-SS-FSM-State": fsm_state,
            "X-SS-Data-Class": data_class.value,
        }
        response, latency_ms = await self._post(route, self._chat_url, body, headers)
        try:
            completion = _Completion.model_validate_json(response.content)
        except ValidationError as exc:
            raise _unavailable(route, "MALFORMED_RESPONSE") from exc
        content = completion.choices[0].message.content
        parsed = None
        if response_format is not None:
            try:
                parsed = response_format.model_validate_json(content)
            except ValidationError as exc:
                raise _unavailable(route, "SCHEMA_MISMATCH") from exc
        logger.debug(
            "gateway %s served by %s -> %d in %.0f ms (%d in, %d out)",
            route,
            completion.model,
            response.status_code,
            latency_ms,
            completion.usage.prompt_tokens,
            completion.usage.completion_tokens,
        )
        return GatewayResult(
            content=content,
            parsed=parsed,
            served_model=completion.model,
            tokens_in=completion.usage.prompt_tokens,
            tokens_out=completion.usage.completion_tokens,
            latency_ms=latency_ms,
        )

    async def embed(
        self,
        texts: list[str],
        *,
        kind: Literal["query", "document"],
        timeout_s: float | None = None,
    ) -> list[list[float]]:
        """One embed_dim vector per text, in order. Queries get the model's instruction prefix.
        timeout_s replaces the route's per-query budget; only batch ingestion passes it."""
        if not texts:
            return []
        inputs = [self._query_prefix + t for t in texts] if kind == "query" else texts
        response, latency_ms = await self._post(
            Route.EMBED,
            self._embed_url,
            {"model": Route.EMBED.value, "input": inputs},
            timeout_s=timeout_s,
        )
        try:
            result = _Embeddings.model_validate_json(response.content)
        except ValidationError as exc:
            raise _unavailable(Route.EMBED, "MALFORMED_RESPONSE") from exc
        data = sorted(result.data, key=lambda e: e.index)
        if [e.index for e in data] != list(range(len(texts))) or any(
            len(e.embedding) != self._embed_dim for e in data
        ):
            raise _unavailable(Route.EMBED, "MALFORMED_RESPONSE")
        logger.debug(
            "gateway embed served by %s: %d %s texts in %.0f ms",
            result.model,
            len(texts),
            kind,
            latency_ms,
        )
        return [e.embedding for e in data]

    async def healthz(self) -> None:
        """Readiness (/readyz): raises unless the gateway answers 2xx. No bearer is sent."""
        response = await self._http.get(self._health_url, timeout=2.0)
        response.raise_for_status()

    async def embed_model(self) -> TeiModel:
        return await self._info(Route.EMBED, self._embed_info_url)

    async def rerank_model(self) -> TeiModel:
        return await self._info(Route.RERANK, self._rerank_info_url)

    async def _info(self, route: Route, url: str) -> TeiModel:
        response, _ = await self._post(route, url, None)
        try:
            info = _Info.model_validate_json(response.content)
        except ValidationError as exc:
            raise _unavailable(route, "MALFORMED_RESPONSE") from exc
        return TeiModel(info.model_id, info.model_sha)

    def _timeout(self, route: Route) -> float:
        scale = self._tei_scale if route in (Route.EMBED, Route.RERANK) else 1.0
        return ROUTES[route].timeout_s * scale

    async def rerank(self, query: str, docs: list[str]) -> list[float]:
        """A relevance score in [0, 1] per doc, in the order given."""
        if not docs:
            return []
        response, latency_ms = await self._post(
            Route.RERANK, self._rerank_url, {"query": query, "texts": docs, "raw_scores": False}
        )
        try:
            hits = _HITS.validate_json(response.content)
        except ValidationError as exc:
            raise _unavailable(Route.RERANK, "MALFORMED_RESPONSE") from exc
        if sorted(h.index for h in hits) != list(range(len(docs))):
            raise _unavailable(Route.RERANK, "MALFORMED_RESPONSE")
        scores = [0.0] * len(docs)
        for hit in hits:
            scores[hit.index] = hit.score
        logger.debug("gateway rerank: %d docs in %.0f ms", len(docs), latency_ms)
        return scores

    async def _post(
        self,
        route: Route,
        url: str,
        body: dict[str, Any] | None,
        headers: dict[str, str] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> tuple[httpx.Response, float]:
        """POST body, or GET when there is none."""
        # Never log the body, the URL or str(exc): they carry customer text and endpoints.
        timeout = timeout_s or self._timeout(route)
        started = time.perf_counter()
        try:
            async with asyncio.timeout(timeout):
                if body is None:
                    response = await self._http.get(url, headers=headers, timeout=timeout)
                else:
                    response = await self._http.post(
                        url, json=body, headers=headers, timeout=timeout
                    )
        except TimeoutError as exc:
            raise _unavailable(route, "TIMEOUT") from exc
        except httpx.TransportError as exc:
            raise _unavailable(route, "UNAVAILABLE") from exc
        latency_ms = (time.perf_counter() - started) * 1000
        compression = response.headers.get(COMPRESSION_HEADER)
        if compression is not None and _compression_fired(compression):
            logger.warning("gateway %s rejected: compression fired", route)
            raise GatewayPolicyViolation("COMPRESSION_APPLIED", response.status_code)
        if response.is_error:
            raise _unavailable(route, "HTTP_ERROR", response.status_code)
        return response, latency_ms


def _check_data_class(
    route: Route,
    data_class: DataClass,
    messages: list[dict[str, str]],
    attestation: RedactionAttestation | None,
) -> None:
    """Raw text only to self-hosted routes; redacted text only with an attestation for exactly
    these messages."""
    reason = None
    if data_class is DataClass.SELF_HOSTED_RAW and not ROUTES[route].self_hosted:
        reason = "DATA_CLASS_DENIED"
    elif data_class is DataClass.REDACTED:
        if attestation is None:
            reason = "ATTESTATION_MISSING"
        elif attestation.envelope_sha256 != sha256_hex(messages):
            reason = "ATTESTATION_MISMATCH"
    if reason is not None:
        logger.warning("gateway %s refused: %s", route, reason)
        raise GatewayPolicyViolation(reason)


def _compression_fired(value: str) -> bool:
    mode, *rest = (part.strip().lower() for part in value.split(";"))
    return mode not in _COMPRESSION_OFF or any(p.startswith(_COMPRESSION_REWRITE) for p in rest)


def _unavailable(route: Route, reason: str, status: int | None = None) -> GatewayUnavailable:
    logger.warning("gateway %s unavailable: %s %s", route, reason, status or "")
    return GatewayUnavailable(reason, status)
