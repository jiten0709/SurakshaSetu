import asyncio
import json
from uuid import UUID

import httpx
import pytest
import respx

from surakshasetu.domain.client import DomainClient, DomainError
from surakshasetu.domain.models import ConsentRecordCreate, ConsentWithdrawal, PurposeGrant

BASE = "http://domain.test"
CONSENT_ID = UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
RECORD = {
    "consent_id": str(CONSENT_ID),
    "notice_version": "2026.09.1-en",
    "notice_sha256": "ab" * 32,
    "notice_language": "en-IN",
    "ai_disclosure_version": "ai-2026.09.1",
    "purposes": [{"purpose_id": "P1_NEEDS_RECO", "granted": True}],
    "age_18_plus_declared": True,
    "method": "structured_action",
    "captured_at": "2026-09-23T10:15:00Z",
    "valid_p1": True,
    "valid_reasons": [],
}


@pytest.mark.asyncio
@respx.mock(base_url=BASE, assert_all_called=True)
async def test_consent_write_sends_token_and_idempotency_key(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post("/v1/consent/records").respond(201, json=RECORD)
    create = ConsentRecordCreate(
        session_id=CONSENT_ID,
        subject_ref=CONSENT_ID,
        notice_version="2026.09.1-en",
        notice_sha256="ab" * 32,
        language="en-IN",
        ai_disclosure_version="ai-2026.09.1",
        purposes=[PurposeGrant(purpose_id="P1_NEEDS_RECO", granted=True)],
        age_18_plus_declared=True,
        method="structured_action",
    )

    async with DomainClient(BASE, "t0ken") as client:
        record = await client.create_consent_record(create, idempotency_key="k-1")

    sent = route.calls.last.request
    assert sent.headers["Authorization"] == "Bearer t0ken"
    assert sent.headers["Idempotency-Key"] == "k-1"
    assert json.loads(sent.content)["method"] == "structured_action"
    assert record.valid_p1


@pytest.mark.asyncio
@respx.mock(base_url=BASE)
async def test_unset_optional_fields_and_params_are_not_sent(respx_mock: respx.MockRouter) -> None:
    withdraw = respx_mock.post(f"/v1/consent/records/{CONSENT_ID}/withdraw").respond(
        200, json=RECORD
    )
    lookup = respx_mock.get(f"/v1/consent/records/{CONSENT_ID}").respond(200, json=RECORD)

    async with DomainClient(BASE, "t") as client:
        await client.withdraw_consent(CONSENT_ID, ConsentWithdrawal())
        await client.get_consent_record(CONSENT_ID)

    assert withdraw.calls.last.request.content == b"{}"
    assert lookup.calls.last.request.url.query == b""


@pytest.mark.asyncio
@respx.mock(base_url=BASE)
async def test_problem_json_becomes_a_typed_error(respx_mock: respx.MockRouter) -> None:
    respx_mock.get("/v1/suitability/required-slots").respond(
        409,
        json={"title": "Conflict", "status": 409, "code": "RULES_VERSION_UNKNOWN"},
        headers={"Content-Type": "application/problem+json"},
    )
    respx_mock.get("/v1/meta/versions").respond(502, text="<html>bad gateway</html>")

    async with DomainClient(BASE, "t") as client:
        with pytest.raises(DomainError) as conflict:
            await client.get_required_slots("r-unknown")
        with pytest.raises(DomainError) as gateway:
            await client.get_versions()

    assert (conflict.value.code, conflict.value.status) == ("RULES_VERSION_UNKNOWN", 409)
    assert conflict.value.problem is not None
    assert respx_mock.calls[0].request.url.params["pins.rules"] == "r-unknown"
    assert (gateway.value.code, gateway.value.status) == ("UNEXPECTED_RESPONSE", 502)


@pytest.mark.asyncio
@respx.mock(base_url=BASE, assert_all_called=False)  # the budget cancels the call mid-flight
async def test_a_lookup_over_its_budget_times_out(respx_mock: respx.MockRouter) -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.5)
        return httpx.Response(200, json=RECORD)

    respx_mock.get(f"/v1/consent/records/{CONSENT_ID}").mock(side_effect=slow)

    async with DomainClient(BASE, "t") as client:
        with pytest.raises(DomainError) as excinfo:
            await client.get_consent_record(CONSENT_ID)

    assert (excinfo.value.code, excinfo.value.status) == ("TIMEOUT", None)
