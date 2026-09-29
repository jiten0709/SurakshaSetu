import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import respx
from pydantic import BaseModel, SecretStr

from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.gateway import (
    DataClass,
    Gateway,
    GatewayPolicyViolation,
    GatewayUnavailable,
    RedactionAttestation,
    Route,
)
from surakshasetu.gateway.adapter import ROUTES, EmbedModel
from surakshasetu.logging import configure_logging

GATEWAY = "http://gateway.test/v1"
EMBED = "http://embed.test"
RERANK = "http://rerank.test"
SESSION = UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
TURN = UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c")
MESSAGES = [{"role": "user", "content": "I am 34"}]


def settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        gateway_base_url=GATEWAY,
        gateway_api_key=SecretStr("g4teway-key"),
        tei_embed_url=EMBED,
        tei_rerank_url=RERANK,
        **overrides,
    )


def completion(content: str = "Hello there.", model: str = "stub-gen") -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
    }


async def call(route: Route, **kwargs: Any) -> Any:
    kwargs.setdefault("data_class", DataClass.SELF_HOSTED_RAW)
    kwargs.setdefault("messages", MESSAGES)
    async with Gateway(settings()) as gateway:
        return await gateway.call(route, session_id=SESSION, turn_id=TURN, fsm_state="S1", **kwargs)


def test_routes_carry_the_tdd_timeouts_and_data_rules() -> None:
    assert {r: (s.timeout_s, s.self_hosted) for r, s in ROUTES.items()} == {
        Route.GUARD_INPUT: (0.2, True),
        Route.NLU_EXTRACT: (0.8, True),
        Route.GEN_CONVERSE: (2.0, False),
        Route.GEN_RECOMMEND: (6.0, False),
        Route.VERIFY_CLAIMS: (1.0, True),
        Route.SUMMARISE: (1.0, True),
        Route.EMBED: (0.15, True),
        Route.RERANK: (0.3, True),
    }


@pytest.mark.asyncio
@respx.mock(assert_all_called=True)
async def test_a_call_names_the_route_and_records_the_served_model(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"{GATEWAY}/chat/completions").respond(
        200, json=completion(model="stub-nlu")
    )

    result = await call(Route.NLU_EXTRACT)

    sent = route.calls.last.request
    body = json.loads(sent.content)
    assert body == {"model": "nlu-extract", "messages": MESSAGES, "user": str(SESSION)}
    assert sent.headers["Authorization"] == "Bearer g4teway-key"
    assert sent.headers["X-SS-Turn-Id"] == str(TURN)
    assert sent.headers["X-SS-FSM-State"] == "S1"
    assert sent.headers["X-SS-Data-Class"] == "self_hosted_raw"
    assert result.served_model == "stub-nlu"
    assert (result.content, result.tokens_in, result.tokens_out) == ("Hello there.", 12, 3)
    assert result.parsed is None
    assert result.fallback_hops == 0
    assert result.latency_ms >= 0


@pytest.mark.asyncio
@respx.mock
async def test_each_route_has_its_own_timeout(respx_mock: respx.MockRouter) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=completion())

    respx_mock.post(f"{GATEWAY}/chat/completions").mock(side_effect=slow)

    with pytest.raises(GatewayUnavailable) as timed_out:
        await call(Route.GUARD_INPUT)  # 200 ms
    result = await call(Route.NLU_EXTRACT)  # 800 ms

    assert timed_out.value.reason == "TIMEOUT"
    assert timed_out.value.status is None
    assert result.content == "Hello there."


@pytest.mark.asyncio
# OmniRoute 3.8.50 sends "<mode>; source=<source>" on every chat response.
@pytest.mark.parametrize(
    "value",
    ["off", "None", "false", "0", "off; source=off", "OFF; source=request-header"],
)
@respx.mock
async def test_a_compression_header_that_did_not_fire_is_accepted(
    respx_mock: respx.MockRouter, value: str
) -> None:
    respx_mock.post(f"{GATEWAY}/chat/completions").respond(
        200, json=completion(), headers={"x-omniroute-compression": value}
    )

    assert (await call(Route.GUARD_INPUT)).content == "Hello there."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "applied",
        "relevance",
        "session-dedup",
        "",
        "standard; source=default",
        "rtk; source=auto-trigger",
        # rules rewrote the prompt after an off plan, with or without the plan in front
        "off; source=off; tokens=812->640; rules: dedupx2",
        "tokens=812->640; rules: dedupx2",
    ],
)
@respx.mock
async def test_a_response_whose_compression_fired_is_rejected(
    respx_mock: respx.MockRouter, value: str
) -> None:
    respx_mock.post(f"{GATEWAY}/chat/completions").respond(
        200, json=completion(), headers={"x-omniroute-compression": value}
    )

    with pytest.raises(GatewayPolicyViolation) as rejected:
        await call(Route.GUARD_INPUT)

    assert rejected.value.reason == "COMPRESSION_APPLIED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "data_class", "attestation", "reason"),
    [
        (Route.GEN_CONVERSE, DataClass.SELF_HOSTED_RAW, None, "DATA_CLASS_DENIED"),
        (Route.GEN_RECOMMEND, DataClass.SELF_HOSTED_RAW, None, "DATA_CLASS_DENIED"),
        (Route.GEN_CONVERSE, DataClass.REDACTED, None, "ATTESTATION_MISSING"),
        (Route.GUARD_INPUT, DataClass.REDACTED, None, "ATTESTATION_MISSING"),
        (
            Route.GEN_RECOMMEND,
            DataClass.REDACTED,
            RedactionAttestation(sha256_hex([{"role": "user", "content": "other text"}])),
            "ATTESTATION_MISMATCH",
        ),
    ],
)
@respx.mock
async def test_the_data_class_policy_refuses_before_any_request(
    respx_mock: respx.MockRouter,
    route: Route,
    data_class: DataClass,
    attestation: RedactionAttestation | None,
    reason: str,
) -> None:
    sent = respx_mock.post(f"{GATEWAY}/chat/completions").respond(200, json=completion())

    with pytest.raises(GatewayPolicyViolation) as refused:
        await call(route, data_class=data_class, attestation=attestation)

    assert refused.value.reason == reason
    assert not sent.called


@pytest.mark.asyncio
@respx.mock
async def test_attested_redacted_messages_may_go_to_any_route(
    respx_mock: respx.MockRouter,
) -> None:
    sent = respx_mock.post(f"{GATEWAY}/chat/completions").respond(200, json=completion())

    for route in (Route.GEN_RECOMMEND, Route.VERIFY_CLAIMS):
        await call(
            route,
            data_class=DataClass.REDACTED,
            attestation=RedactionAttestation(sha256_hex(MESSAGES)),
        )

    assert sent.call_count == 2
    assert sent.calls.last.request.headers["X-SS-Data-Class"] == "redacted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason", "status"),
    [
        (httpx.Response(503, json={"error": {"message": "down"}}), "HTTP_ERROR", 503),
        (httpx.Response(401, json={"error": {"message": "no"}}), "HTTP_ERROR", 401),
        (httpx.Response(200, text="not json"), "MALFORMED_RESPONSE", None),
        (httpx.Response(200, json={"model": "m", "choices": []}), "MALFORMED_RESPONSE", None),
        (
            httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]}),
            "MALFORMED_RESPONSE",
            None,
        ),
    ],
)
@respx.mock
async def test_failures_become_gateway_unavailable_with_a_reason(
    respx_mock: respx.MockRouter, response: httpx.Response, reason: str, status: int | None
) -> None:
    respx_mock.post(f"{GATEWAY}/chat/completions").mock(return_value=response)

    with pytest.raises(GatewayUnavailable) as failed:
        await call(Route.GUARD_INPUT)

    assert (failed.value.reason, failed.value.status) == (reason, status)
    assert not isinstance(failed.value, GatewayPolicyViolation)


@pytest.mark.asyncio
@respx.mock
async def test_an_unreachable_gateway_is_unavailable(respx_mock: respx.MockRouter) -> None:
    respx_mock.post(f"{GATEWAY}/chat/completions").mock(side_effect=httpx.ConnectError("refused"))

    with pytest.raises(GatewayUnavailable) as failed:
        await call(Route.GUARD_INPUT)

    assert failed.value.reason == "UNAVAILABLE"


class GuardVerdict(BaseModel):
    injection_score: float
    safety: str


@pytest.mark.asyncio
@respx.mock
async def test_response_format_is_sent_as_a_json_schema_and_parsed(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"{GATEWAY}/chat/completions").respond(
        200, json=completion('{"injection_score": 0.01, "safety": "safe"}')
    )

    result = await call(Route.GUARD_INPUT, response_format=GuardVerdict)

    sent = json.loads(route.calls.last.request.content)["response_format"]
    assert sent["type"] == "json_schema"
    assert sent["json_schema"]["name"] == "GuardVerdict"
    assert sent["json_schema"]["schema"] == GuardVerdict.model_json_schema()
    assert result.parsed == GuardVerdict(injection_score=0.01, safety="safe")


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ['{"injection_score": "high"}', "safe", ""])
@respx.mock
async def test_content_failing_the_response_format_is_unavailable(
    respx_mock: respx.MockRouter, content: str
) -> None:
    respx_mock.post(f"{GATEWAY}/chat/completions").respond(200, json=completion(content))

    with pytest.raises(GatewayUnavailable) as failed:
        await call(Route.GUARD_INPUT, response_format=GuardVerdict)

    assert failed.value.reason == "SCHEMA_MISMATCH"


# --- embed and rerank: TEI, called directly --------------------------------------------------
def embeddings(*vectors: list[float], order: list[int] | None = None) -> dict[str, Any]:
    indexes = order or list(range(len(vectors)))
    return {
        "object": "list",
        "model": "embed-model",
        "data": [{"object": "embedding", "index": i, "embedding": vectors[i]} for i in indexes],
        "usage": {"prompt_tokens": 4, "total_tokens": 4},
    }


@pytest.mark.asyncio
@respx.mock(assert_all_called=True)
async def test_embed_goes_to_tei_directly_and_keeps_input_order(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"{EMBED}/v1/embeddings").respond(
        200, json=embeddings([1.0, 0.0], [0.0, 1.0], order=[1, 0])
    )

    async with Gateway(settings(embed_dim=2)) as gateway:
        vectors = await gateway.embed(["a", "b"], kind="document")

    sent = route.calls.last.request
    assert json.loads(sent.content) == {"model": "embed", "input": ["a", "b"]}
    assert "Authorization" not in sent.headers
    assert vectors == [[1.0, 0.0], [0.0, 1.0]]


@pytest.mark.asyncio
@respx.mock
async def test_the_query_prefix_goes_on_queries_only(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{EMBED}/v1/embeddings").respond(200, json=embeddings([1.0]))

    async with Gateway(settings(embed_dim=1, embed_query_prefix="Q: ")) as gateway:
        await gateway.embed(["exclusions?"], kind="query")
        query = json.loads(route.calls.last.request.content)["input"]
        await gateway.embed(["5.3 Exclusions"], kind="document")
        document = json.loads(route.calls.last.request.content)["input"]

    assert (query, document) == (["Q: exclusions?"], ["5.3 Exclusions"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        embeddings([1.0, 0.0, 0.0]),  # wrong dimension: another model is being served
        embeddings([1.0, 0.0], [1.0, 0.0], order=[0, 0]),  # an index repeated
        embeddings(),  # a vector missing
        {"data": "nope"},
    ],
)
@respx.mock
async def test_embeddings_of_the_wrong_shape_are_refused(
    respx_mock: respx.MockRouter, body: dict[str, Any]
) -> None:
    respx_mock.post(f"{EMBED}/v1/embeddings").respond(200, json=body)

    async with Gateway(settings(embed_dim=2)) as gateway:
        with pytest.raises(GatewayUnavailable) as failed:
            await gateway.embed(["a"], kind="query")

    assert failed.value.reason == "MALFORMED_RESPONSE"


@pytest.mark.asyncio
@respx.mock
async def test_embed_and_rerank_time_out_on_their_own_budgets(
    respx_mock: respx.MockRouter,
) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=[{"index": 0, "score": 0.5}])

    respx_mock.post(f"{EMBED}/v1/embeddings").mock(side_effect=slow)
    respx_mock.post(f"{RERANK}/rerank").mock(side_effect=slow)

    async with Gateway(settings()) as gateway:
        with pytest.raises(GatewayUnavailable) as embed_failed:
            await gateway.embed(["a"], kind="query")  # 150 ms
        scores = await gateway.rerank("q", ["a"])  # 300 ms

    assert embed_failed.value.reason == "TIMEOUT"
    assert scores == [0.5]


@pytest.mark.asyncio
@respx.mock
async def test_batch_ingestion_can_widen_the_embed_budget(respx_mock: respx.MockRouter) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=embeddings([1.0]))

    respx_mock.post(f"{EMBED}/v1/embeddings").mock(side_effect=slow)

    async with Gateway(settings(embed_dim=1)) as gateway:
        vectors = await gateway.embed(["a"], kind="document", timeout_s=1.0)

    assert vectors == [[1.0]]


@pytest.mark.asyncio
@respx.mock(assert_all_called=True)
async def test_embed_model_reads_tei_info(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(f"{EMBED}/info").respond(
        200, json={"model_id": "org/model", "model_sha": "abc123", "max_input_length": 8192}
    )

    async with Gateway(settings()) as gateway:
        served = await gateway.embed_model()

    assert served == EmbedModel(model_id="org/model", model_sha="abc123")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [httpx.Response(200, json={"x": 1}), httpx.Response(503)])
@respx.mock
async def test_embed_model_failures_are_unavailable(
    respx_mock: respx.MockRouter, response: httpx.Response
) -> None:
    respx_mock.get(f"{EMBED}/info").mock(return_value=response)

    async with Gateway(settings()) as gateway:
        with pytest.raises(GatewayUnavailable):
            await gateway.embed_model()


@pytest.mark.asyncio
@respx.mock(assert_all_called=True)
async def test_rerank_scores_come_back_in_input_order(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{RERANK}/rerank").respond(
        200,
        json=[{"index": 2, "score": 0.9}, {"index": 0, "score": 0.4}, {"index": 1, "score": 0.1}],
    )

    async with Gateway(settings()) as gateway:
        scores = await gateway.rerank("what is excluded?", ["a", "b", "c"])

    assert json.loads(route.calls.last.request.content) == {
        "query": "what is excluded?",
        "texts": ["a", "b", "c"],
        "raw_scores": False,
    }
    assert scores == [0.4, 0.1, 0.9]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body", [[{"index": 0, "score": 0.9}], [{"index": 0}, {"index": 1}], {"error": "x"}]
)
@respx.mock
async def test_rerank_results_of_the_wrong_shape_are_refused(
    respx_mock: respx.MockRouter, body: Any
) -> None:
    respx_mock.post(f"{RERANK}/rerank").respond(200, json=body)

    async with Gateway(settings()) as gateway:
        with pytest.raises(GatewayUnavailable) as failed:
            await gateway.rerank("q", ["a", "b"])

    assert failed.value.reason == "MALFORMED_RESPONSE"


@pytest.mark.asyncio
@respx.mock(assert_all_called=False)
async def test_empty_inputs_make_no_call(respx_mock: respx.MockRouter) -> None:
    embed = respx_mock.post(f"{EMBED}/v1/embeddings")
    rerank = respx_mock.post(f"{RERANK}/rerank")

    async with Gateway(settings()) as gateway:
        assert await gateway.embed([], kind="query") == []
        assert await gateway.rerank("q", []) == []

    assert not embed.called and not rerank.called


# --- logs --------------------------------------------------------------------------------------
def _files_text(log_dir: Path) -> str:
    return "".join(path.read_text() for path in log_dir.glob("*.log"))


@pytest.mark.asyncio
@pytest.mark.usefixtures("restore_logging")
@respx.mock
async def test_model_calls_leak_no_text_key_or_url_into_the_logs(
    respx_mock: respx.MockRouter, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging("DEBUG", tmp_path)
    said = [{"role": "user", "content": "my PAN is ABCDE1234F"}]
    respx_mock.post(f"{GATEWAY}/chat/completions").respond(
        200, json=completion("Your PAN ABCDE1234F is noted.", model="stub-guard")
    )
    respx_mock.post(f"{EMBED}/v1/embeddings").respond(200, json=embeddings([1.0]))
    respx_mock.post(f"{RERANK}/rerank").mock(side_effect=httpx.ConnectError("rerank.test"))

    async with Gateway(settings(embed_dim=1)) as gateway:
        await gateway.call(
            Route.GUARD_INPUT,
            data_class=DataClass.SELF_HOSTED_RAW,
            messages=said,
            session_id=SESSION,
            turn_id=TURN,
            fsm_state="S1",
        )
        await gateway.embed(["ABCDE1234F"], kind="query")
        with pytest.raises(GatewayUnavailable):
            await gateway.rerank("ABCDE1234F", ["ABCDE1234F"])

    logged = capsys.readouterr().out + _files_text(tmp_path)
    assert "guard-input" in logged and "stub-guard" in logged and "UNAVAILABLE" in logged
    for leaked in ("ABCDE1234F", "g4teway-key", "gateway.test", "embed.test", "rerank.test"):
        assert leaked not in logged
