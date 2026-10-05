"""The stub application journey (Step 21): a payload signed with the orchestrator's dev key is taken
once and given a reference; a bad signature, a replay and a scripted failure are refused."""

import base64
from collections.abc import Iterator
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

import journey
from app import app

client = TestClient(app)
# The orchestrator's dev signing key (its Settings.intake_signing_key_b64), public bytes.
DEV_KEY = Ed25519PrivateKey.from_private_bytes(b"surakshasetu-dev-intake-signing!")


@pytest.fixture(autouse=True)
def empty() -> Iterator[None]:
    yield
    journey._taken.clear()
    journey._accepted.clear()
    journey._failures.clear()


def signed(key: Ed25519PrivateKey = DEV_KEY, **update: Any) -> dict[str, Any]:
    payload = {
        "session_id": "s-1",
        "subject_ref": "subj-1",
        "selected": {
            "uin": "999N001V02",
            "quote_id": "Q-2026-10-05-0001",
            "sum_assured_inr": 37500000,
            "term_years": 26,
            "ppt": "regular",
            "rider_uins": ["999A007V01"],
        },
        "confirmed_slots": {"age": 34, "tobacco_12m": False, "annual_income_inr": 2400000},
        "suitability_inputs_sha256": "a" * 64,
        "disclosure_acks": [
            {"uin": "999N001V02", "registry_version": "2026.09.1", "set_sha256": "b" * 64}
        ],
        "audit_anchor": {"session_seq": 42, "hash": "c" * 64},
    } | update
    signature = key.sign(journey.canonical(payload))
    return {**payload, "signature": base64.b64encode(signature).decode()}


def test_the_dev_public_key_is_the_orchestrators() -> None:
    raw = DEV_KEY.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert base64.b64encode(raw).decode() == journey.DEV_PUBLIC_KEY_B64


def test_a_signed_intake_is_taken_and_given_a_reference() -> None:
    response = client.post("/journey/intake", json=signed())

    assert response.status_code == 201
    ref = response.json()["intake_ref"]
    seen = client.get("/journey/__intake/s-1").json()
    assert seen["intake_ref"] == ref and seen["payload"]["selected"]["rider_uins"] == ["999A007V01"]


def test_a_tampered_payload_fails_the_signature() -> None:
    body = signed()
    body["selected"]["sum_assured_inr"] = 1

    assert client.post("/journey/intake", json=body).status_code == 401
    assert client.get("/journey/__intake/s-1").status_code == 404


def test_another_key_fails_the_signature() -> None:
    other = Ed25519PrivateKey.from_private_bytes(bytes(32))

    assert client.post("/journey/intake", json=signed(other)).status_code == 401


def test_a_replay_of_the_session_and_quote_is_refused_with_the_first_reference() -> None:
    first = client.post("/journey/intake", json=signed()).json()["intake_ref"]

    again = client.post("/journey/intake", json=signed())

    assert again.status_code == 409
    assert again.json() == {"code": "REPLAY", "intake_ref": first}


def test_a_scripted_failure_refuses_the_next_intake_only() -> None:
    client.post("/journey/__fail", json={"session_id": "s-1", "status": 503})

    assert client.post("/journey/intake", json=signed()).status_code == 503
    assert client.post("/journey/intake", json=signed()).status_code == 201


def test_a_malformed_body_is_refused() -> None:
    assert client.post("/journey/intake", json={"session_id": "s-1"}).status_code == 400
