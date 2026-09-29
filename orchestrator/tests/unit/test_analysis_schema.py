"""TDD §3.3: TurnAnalysis/SlotCandidate copied verbatim, and no consent-shaped field or intent
anywhere -- consent is only ever set by the Consent Service. Also covers analysis.nlu's
evidence-span enforcement.
"""

import json
from uuid import UUID

import pytest
import respx
from pydantic import SecretStr

from surakshasetu.analysis.models import Intent, SlotCandidate, TurnAnalysis
from surakshasetu.analysis.nlu import PendingSlotSpec, extract
from surakshasetu.config import Settings
from surakshasetu.gateway import Gateway

GATEWAY = "http://gateway.test/v1"
SESSION = UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b")
TURN = UUID("0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5c")


def test_no_consent_intent_exists() -> None:
    assert not any("CONSENT" in member.value for member in Intent)


def test_no_consent_field_on_slot_candidate_or_turn_analysis() -> None:
    fields = set(SlotCandidate.model_fields) | set(TurnAnalysis.model_fields)
    assert not any("consent" in name.lower() for name in fields)


def test_turn_analysis_defaults_match_the_tdd() -> None:
    analysis = TurnAnalysis(intents=[Intent.SLOT_ANSWER], language="en")
    assert analysis.slots == []
    assert analysis.side_query is None


def _completion(content: str) -> dict[str, object]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "stub-nlu",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
    }


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        gateway_base_url=GATEWAY,
        gateway_api_key=SecretStr("g4teway-key"),
        tei_embed_url="http://embed.test",
        tei_rerank_url="http://rerank.test",
    )


@pytest.mark.asyncio
@respx.mock
async def test_a_slot_candidate_whose_evidence_span_is_not_in_the_text_is_dropped(
    respx_mock: respx.MockRouter,
) -> None:
    text = "I am 34 years old"
    payload = TurnAnalysis(
        intents=[Intent.SLOT_ANSWER],
        slots=[
            SlotCandidate(slot="age", value=34, confidence=0.9, evidence_span="34 years old"),
            SlotCandidate(
                slot="annual_income_inr", value=500000, confidence=0.7, evidence_span="5 lakh"
            ),
        ],
        language="en",
    )
    respx_mock.post(f"{GATEWAY}/chat/completions").respond(
        200, json=_completion(payload.model_dump_json())
    )

    async with Gateway(_settings()) as gateway:
        analysis = await extract(
            gateway,
            text=text,
            pending=PendingSlotSpec(pending_slot="age"),
            session_id=SESSION,
            turn_id=TURN,
            fsm_state="S1",
        )

    assert [s.slot for s in analysis.slots] == ["age"]


@pytest.mark.asyncio
@respx.mock
async def test_the_pending_slot_and_known_slots_reach_the_system_message(
    respx_mock: respx.MockRouter,
) -> None:
    route = respx_mock.post(f"{GATEWAY}/chat/completions").respond(
        200,
        json=_completion(TurnAnalysis(intents=[], language="en").model_dump_json()),
    )

    async with Gateway(_settings()) as gateway:
        await extract(
            gateway,
            text="34",
            pending=PendingSlotSpec(pending_slot="age", known_slots=("age", "pincode")),
            session_id=SESSION,
            turn_id=TURN,
            fsm_state="S1",
        )

    sent = json.loads(route.calls.last.request.content)
    system_message = sent["messages"][0]["content"]
    assert "age" in system_message
    assert "pincode" in system_message
