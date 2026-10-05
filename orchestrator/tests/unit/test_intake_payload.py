"""The hand-off adapter's intake payload (Step 21): TDD §7.1 field for field, signed with Ed25519
over JCS(the payload without its signature), verified with the public key; a tampered payload or
another key fails; the journey's replies (taken, replayed, refused, down) as the adapter reads
them."""

import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import canonical_json
from surakshasetu.domain.models import PremiumQuote
from surakshasetu.graph.state import DisclosureAck
from surakshasetu.handoff import intake

REPO = Path(__file__).resolve().parents[3]
KEY = intake.signing_key(Settings(_env_file=None).intake_signing_key_b64.get_secret_value())
PUBLIC = intake.public_key_b64(KEY)
SET = "c41d" * 16
# TDD §7.1's example, as JSON types (its values elided with … are hashes and ids).
TDD = {
    "session_id": str,
    "subject_ref": str,
    "selected": {
        "uin": str,
        "quote_id": str,
        "sum_assured_inr": int,
        "term_years": int,
        "ppt": str,
        "rider_uins": [str],
    },
    "confirmed_slots": {"age": int, "tobacco_12m": bool, "annual_income_inr": int},
    "suitability_inputs_sha256": str,
    "disclosure_acks": [{"uin": str, "registry_version": str, "set_sha256": str}],
    "audit_anchor": {"session_seq": int, "hash": str},
    "signature": str,
}


def quote(**update: Any) -> PremiumQuote:
    base = {
        "decision_id": "0199a1b2-0000-7000-8000-0000000004a0",
        "quote_id": "Q-2026-10-05-0005",
        "uin": "999N001V02",
        "sum_assured_inr": "37500000",
        "term_years": 26,
        "ppt": "regular",
        "annual_premium_inr": "71250",
        "frequency": "annual",
        "valid_until": "2026-11-04",
        "indicative": True,
        "rider_premiums": {"999A007V01": "11250"},
        "gst_included": True,
        "rating_version": "rating-dummy-2026.09.1",
        "inputs_sha256": "ab" * 32,
        "reason_codes": [],
    }
    return PremiumQuote.model_validate(base | update)


def ack(uin: str = "999N001V02") -> DisclosureAck:
    return DisclosureAck(
        uin=uin,
        registry_version="2026.09.1",
        disclosure_set_sha256=SET,
        document_sha256={"CIS": "11" * 32, "POLICY_WORDING": "22" * 32},
        acked_at=datetime(2026, 10, 5, 10, 0, tzinfo=UTC),
    )


def payload(**update: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "session_id": "0199a1b2-0000-7000-8000-00000000c0de",
        "subject_ref": "0199a1b2-0000-7000-8000-00000000beef",
        "quote": quote(),
        "slots": {"age_years": 34, "tobacco_12m": False, "annual_income_inr": "2400000"},
        "suitability_inputs_sha256": "3f9a" * 16,
        "acks": [ack()],
        "anchor": (42, bytes.fromhex("7d02" * 16)),
    }
    return intake.build(**(args | update))


def shape(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: shape(v) for k, v in value.items()}
    if isinstance(value, list):
        return [shape(value[0])] if value else []
    return type(value)


def test_the_payload_is_tdd_7_1_field_for_field() -> None:
    signed = intake.sign(payload(), KEY)

    assert shape(signed) == TDD
    assert list(signed) == list(TDD)  # the TDD's order too (JCS sorts it anyway)


def test_selected_is_the_chosen_quote_and_its_riders() -> None:
    p = payload(quote=quote(rider_premiums={"999A009V01": "30000", "999A007V01": "11250"}))

    assert p["selected"] == {
        "uin": "999N001V02",
        "quote_id": "Q-2026-10-05-0005",
        "sum_assured_inr": 37500000,
        "term_years": 26,
        "ppt": "regular",
        "rider_uins": ["999A007V01", "999A009V01"],
    }
    assert p["confirmed_slots"] == {"age": 34, "tobacco_12m": False, "annual_income_inr": 2400000}
    assert p["disclosure_acks"] == [
        {"uin": "999N001V02", "registry_version": "2026.09.1", "set_sha256": SET}
    ]
    assert p["audit_anchor"] == {"session_seq": 42, "hash": "7d02" * 16}


def test_a_declined_income_is_null_and_paise_are_refused() -> None:
    p = payload(slots={"age_years": 34, "tobacco_12m": False, "annual_income_inr": None})
    assert p["confirmed_slots"]["annual_income_inr"] is None
    with pytest.raises(ValueError, match="paise"):
        payload(quote=quote(sum_assured_inr="37500000.50"))


def test_the_signature_is_ed25519_over_jcs_of_the_rest() -> None:
    signed = intake.sign(payload(), KEY)

    body = {k: v for k, v in signed.items() if k != "signature"}
    KEY.public_key().verify(base64.b64decode(signed["signature"]), canonical_json(body))
    assert intake.verify(signed, PUBLIC)


@pytest.mark.parametrize(
    "path",
    [
        ("selected", "sum_assured_inr"),
        ("selected", "rider_uins"),
        ("confirmed_slots", "age"),
        ("disclosure_acks", 0, "set_sha256"),
        ("audit_anchor", "session_seq"),
        ("suitability_inputs_sha256",),
    ],
)
def test_a_tampered_payload_fails_verification(path: tuple[Any, ...]) -> None:
    signed = json.loads(json.dumps(intake.sign(payload(), KEY)))
    node = signed
    for step in path[:-1]:
        node = node[step]
    value = node[path[-1]]
    node[path[-1]] = (
        value + 1 if isinstance(value, int) else ["x"] if isinstance(value, list) else "0"
    )

    assert not intake.verify(signed, PUBLIC)


def test_another_key_or_no_signature_fails() -> None:
    other = Ed25519PrivateKey.from_private_bytes(bytes(32))
    assert not intake.verify(intake.sign(payload(), other), PUBLIC)
    assert not intake.verify(payload(), PUBLIC)


def test_the_stub_journey_holds_the_dev_public_key() -> None:
    """tools/stubs/journey.py and infra/compose.yaml verify with the dev key's public half."""
    stub = (REPO / "tools" / "stubs" / "journey.py").read_text()
    compose = (REPO / "infra" / "compose.yaml").read_text()
    assert re.search(rf'DEV_PUBLIC_KEY_B64 = "{re.escape(PUBLIC)}"', stub)
    assert f"INTAKE_PUBLIC_KEY_B64:-{PUBLIC}" in compose


def transport(status: int, body: dict[str, Any] | None = None) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(status, json=body or {}))


@pytest.mark.asyncio
async def test_the_journey_takes_it_and_a_replay_is_the_same_intake() -> None:
    signed = intake.sign(payload(), KEY)

    taken = await intake.post(
        "http://journey.test/intake", signed, transport=transport(201, {"intake_ref": "INT-1"})
    )
    again = await intake.post(
        "http://journey.test/intake",
        signed,
        transport=transport(409, {"code": "REPLAY", "intake_ref": "INT-1"}),
    )

    assert taken == intake.Accepted("INT-1", replayed=False)
    assert again == intake.Accepted("INT-1", replayed=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (transport(503), "HTTP_503"),
        (transport(401, {"code": "BAD_SIGNATURE"}), "HTTP_401"),
        (transport(201, {"no": "ref"}), "MALFORMED_RESPONSE"),
        (
            httpx.MockTransport(lambda r: (_ for _ in ()).throw(httpx.ConnectError("down"))),
            "UNAVAILABLE",
        ),
    ],
)
async def test_a_journey_that_does_not_take_it_is_unavailable(
    reply: httpx.MockTransport, reason: str
) -> None:
    with pytest.raises(intake.IntakeUnavailable) as raised:
        await intake.post(
            "http://journey.test/intake", intake.sign(payload(), KEY), transport=reply
        )
    assert raised.value.reason == reason
