"""The phase-1 target of the S3 -> S4 row (TDD §7.1): a signed intake payload posted to the existing
application journey, which verifies it and refuses replays.

The payload is TDD §7.1 field for field. It carries hashes, not copies, of the disclosures (the
registry stays the single source), and an anchor into the session's audit chain. `selected` is the
chosen, still-valid quote: its id, cover, term and PPT, and its riders (the rider_premiums keys).
Rupee amounts are whole numbers, as in the TDD's example. The orchestrator signs it with Ed25519
over JCS(payload without signature) using SS_INTAKE_SIGNING_KEY_B64 (a KMS key in prod).

`post` never retries. A 409 from the journey is a replay of (session_id, quote_id), already taken:
it is the same intake, so a retried turn is safe. Nothing here logs the payload.
"""

import base64
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from surakshasetu.crypto.jcs import canonical_json
from surakshasetu.domain.models import PremiumQuote
from surakshasetu.graph.state import DisclosureAck

logger = logging.getLogger(__name__)

INTAKE_BUDGET_S = 2.0  # the application journey is outside the platform: one call, no retry


class IntakeUnavailable(Exception):
    """The journey did not take the intake. reason: TIMEOUT, UNAVAILABLE or HTTP_<status>."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Accepted:
    intake_ref: str
    replayed: bool  # the journey already had it (a retried turn)


def rupees(amount: str | Decimal | None) -> int | None:
    """A Money string as whole rupees; paise would be lost, so they are refused."""
    if amount is None:
        return None
    value = Decimal(amount)
    if value != value.to_integral_value():
        raise ValueError("an intake amount has paise")
    return int(value)


def build(
    *,
    session_id: str,
    subject_ref: str,
    quote: PremiumQuote,
    slots: dict[str, Any],
    suitability_inputs_sha256: str,
    acks: list[DisclosureAck],
    anchor: tuple[int, bytes],
) -> dict[str, Any]:
    """TDD §7.1's payload, unsigned. `slots` are the confirmed slot values; `acks` the chosen UIN's
    acknowledgments; `anchor` the session's latest audit event (seq, hash)."""
    seq, digest = anchor
    return {
        "session_id": session_id,
        "subject_ref": subject_ref,
        "selected": {
            "uin": quote.uin,
            "quote_id": quote.quote_id,
            "sum_assured_inr": rupees(quote.sum_assured_inr),
            "term_years": quote.term_years,
            "ppt": quote.ppt,
            "rider_uins": sorted(quote.rider_premiums),
        },
        "confirmed_slots": {
            "age": slots.get("age_years"),
            "tobacco_12m": slots.get("tobacco_12m"),
            "annual_income_inr": rupees(slots.get("annual_income_inr")),
        },
        "suitability_inputs_sha256": suitability_inputs_sha256,
        "disclosure_acks": [
            {
                "uin": a.uin,
                "registry_version": a.registry_version,
                "set_sha256": a.disclosure_set_sha256,
            }
            for a in acks
        ],
        "audit_anchor": {"session_seq": seq, "hash": digest.hex()},
    }


def signing_key(seed_b64: str) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed_b64))


def public_key_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def sign(payload: dict[str, Any], key: Ed25519PrivateKey) -> dict[str, Any]:
    signature = key.sign(canonical_json(payload))
    return {**payload, "signature": base64.b64encode(signature).decode()}


def verify(signed: dict[str, Any], public_key_b64_: str) -> bool:
    """What the journey does (and tools/stubs/journey.py): the signature over JCS(the rest)."""
    body = {k: v for k, v in signed.items() if k != "signature"}
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64_))
    try:
        key.verify(base64.b64decode(signed.get("signature", "")), canonical_json(body))
    except (InvalidSignature, ValueError):
        return False
    return True


async def post(
    url: str, signed: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None
) -> Accepted:
    try:
        async with httpx.AsyncClient(timeout=INTAKE_BUDGET_S, transport=transport) as client:
            response = await client.post(url, json=signed)
    except httpx.TimeoutException as exc:
        logger.warning("intake not delivered: TIMEOUT")
        raise IntakeUnavailable("TIMEOUT") from exc
    except httpx.TransportError as exc:
        logger.warning("intake not delivered: UNAVAILABLE (%s)", type(exc).__name__)
        raise IntakeUnavailable("UNAVAILABLE") from exc
    if response.status_code in (200, 201, 409):
        try:
            ref = str(response.json()["intake_ref"])
        except (ValueError, KeyError, TypeError) as exc:
            raise IntakeUnavailable("MALFORMED_RESPONSE") from exc
        replayed = response.status_code == 409
        logger.info("intake %s by the journey: %s", "replayed" if replayed else "accepted", ref)
        return Accepted(ref, replayed)
    # A 4xx means the journey refused what we signed (a defect here or a key mismatch), a 5xx that
    # it is down: either way the intake is kept for a retry.
    level = logging.ERROR if response.status_code < 500 else logging.WARNING
    logger.log(level, "intake refused by the journey: %d", response.status_code)
    raise IntakeUnavailable(f"HTTP_{response.status_code}")
