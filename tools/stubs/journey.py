"""Stand-in for the existing application journey: the phase-1 target of the S3 -> S4 row (TDD §7.1).

POST /journey/intake takes the orchestrator's signed intake payload. It verifies the Ed25519
signature over JCS(the payload without its signature) with the orchestrator's public key, refuses a
replay of (session_id, quote_id) with 409 and the reference first issued, and returns an intake
reference. It logs the reference only, never the payload.

JCS here is json.dumps with sorted keys and no whitespace: equal to RFC 8785 for this payload,
which holds only strings, integers, booleans, nulls, lists and objects with ASCII keys.

Test hooks (golden conversations): POST /journey/__fail fails the next intake of a session with a
status; GET /journey/__intake/{session_id} shows what was accepted; DELETE clears the session.
"""

import base64
import binascii
import json
import logging
import os
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger("uvicorn.error")

# The public key of the orchestrator's dev signing key (Settings.intake_signing_key_b64); a real
# journey holds the KMS key's. orchestrator/tests/unit/test_intake_payload.py keeps them equal.
DEV_PUBLIC_KEY_B64 = "Cs0xZpIyN1r+Rjn/+6T9QX2+w26nb4rVbZKwy0wWPKo="


class Failure(BaseModel):
    session_id: str
    status: int = 503


_taken: dict[tuple[str, str], str] = {}  # (session_id, quote_id) -> intake reference
_accepted: dict[str, dict[str, Any]] = {}  # session_id -> {"intake_ref", "payload"}
_failures: dict[str, int] = {}


def canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verified(signed: dict[str, Any], public_key_b64: str) -> bool:
    body = {k: v for k, v in signed.items() if k != "signature"}
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(str(signed.get("signature", ""))), canonical(body))
    except (InvalidSignature, ValueError, binascii.Error):
        return False
    return True


@router.post("/journey/intake")
async def intake(request: Request) -> Response:
    try:
        signed = await request.json()
        session_id = str(signed["session_id"])
        quote_id = str(signed["selected"]["quote_id"])
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"code": "MALFORMED"}, status_code=400)
    if (status := _failures.pop(session_id, None)) is not None:
        return JSONResponse({"code": "SCRIPTED_FAILURE"}, status_code=status)
    if not verified(signed, os.environ.get("INTAKE_PUBLIC_KEY_B64", DEV_PUBLIC_KEY_B64)):
        logger.warning("intake refused: bad signature")
        return JSONResponse({"code": "BAD_SIGNATURE"}, status_code=401)
    if (ref := _taken.get((session_id, quote_id))) is not None:
        logger.info("intake replay refused: %s", ref)
        return JSONResponse({"code": "REPLAY", "intake_ref": ref}, status_code=409)
    ref = f"INT-{len(_taken) + 1:06d}"
    _taken[(session_id, quote_id)] = ref
    _accepted[session_id] = {"intake_ref": ref, "payload": signed}
    logger.info("verified intake %s", ref)
    return JSONResponse({"intake_ref": ref}, status_code=201)


@router.post("/journey/__fail")
async def fail_next(failure: Failure) -> dict[str, int]:
    _failures[failure.session_id] = failure.status
    return {"status": failure.status}


@router.get("/journey/__intake/{session_id}")
async def accepted(session_id: str) -> Response:
    if session_id not in _accepted:
        return JSONResponse({"code": "NOT_FOUND"}, status_code=404)
    return JSONResponse(_accepted[session_id])


@router.delete("/journey/__intake/{session_id}", status_code=204)
async def forget(session_id: str) -> None:
    _accepted.pop(session_id, None)
    _failures.pop(session_id, None)
