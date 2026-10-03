"""Conversation API routes (Step 16): sessions, turns, the event stream, and the internal audit
and kill-switch endpoints. Every error is application/problem+json (api/app.py)."""

import hmac
import logging
from collections.abc import AsyncIterable
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, ConfigDict, Field, model_validator

from surakshasetu.graph.runtime import ProblemError, Runtime
from surakshasetu.logging import session_id_ctx
from surakshasetu.store.conv import SessionRow

logger = logging.getLogger(__name__)

router = APIRouter()

Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateSession(_Strict):
    channel: Literal["web", "app"]
    locale: Literal["en-IN", "hi-IN"]


class Action(_Strict):
    type: Code
    payload: dict[str, Any] = {}


class TurnRequest(_Strict):
    text: Annotated[str, Field(min_length=1)] | None = None
    action: Action | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.text is None) == (self.action is None):
            raise ValueError("send exactly one of text and action")
        return self


class KillSwitchRequest(_Strict):
    kind: Literal["product", "prompt_bundle", "route"]
    target: Annotated[str, Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
    active: bool = True
    reason_code: Code


def runtime(request: Request) -> Runtime:
    rt: Runtime = request.app.state.runtime
    return rt


def bearer(authorization: str | None) -> str | None:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


async def session_auth(
    session_id: UUID,
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> SessionRow:
    row = await runtime(request).authenticate(session_id, bearer(authorization))
    session_id_ctx.set(str(row.session_id))
    return row


def role(name: Literal["ops", "compliance"]) -> Any:
    """Per-role dev API keys (SSO in prod): no or a wrong key is 401, another role's key 403."""

    def check(request: Request, authorization: Annotated[str | None, Header()] = None) -> str:
        settings = runtime(request).settings
        keys = {
            "ops": settings.ops_api_key.get_secret_value(),
            "compliance": settings.compliance_api_key.get_secret_value(),
        }
        presented = (bearer(authorization) or "").encode()
        matched = [r for r, key in keys.items() if hmac.compare_digest(key.encode(), presented)]
        if not matched:
            raise ProblemError(401, "UNAUTHORIZED")
        if matched != [name]:
            raise ProblemError(403, "FORBIDDEN")
        return name

    return Depends(check)


@router.post("/v1/sessions", status_code=201)
async def create_session(body: CreateSession, request: Request) -> JSONResponse:
    created = await runtime(request).create_session(body.channel, body.locale)
    return JSONResponse(created, status_code=201)


@router.post("/v1/sessions/{session_id}/turns")
async def post_turn(
    body: TurnRequest,
    request: Request,
    row: Annotated[SessionRow, Depends(session_auth)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> Response:
    try:
        turn_key = UUID(idempotency_key or "")
    except ValueError:
        raise ProblemError(400, "BAD_REQUEST") from None
    action = body.action.model_dump(mode="json") if body.action else None
    released = await runtime(request).run_turn(row, turn_key, body.text, action)
    return Response(released, media_type="application/json")


@router.get("/v1/sessions/{session_id}/events", response_class=EventSourceResponse)
async def events(
    request: Request, row: Annotated[SessionRow, Depends(session_auth)]
) -> AsyncIterable[ServerSentEvent]:
    """turn.status while a turn runs, and turn.released with the whole message after its commit.
    Tokens are never streamed."""
    async for event, data in runtime(request).gate.subscribe(row.session_id):
        yield ServerSentEvent(event=event, data=data)


@router.get("/internal/audit/sessions/{session_id}/verify")
async def verify_audit(
    session_id: UUID, request: Request, _role: str = role("compliance")
) -> dict[str, Any]:
    result = await runtime(request).verify(session_id)
    return {
        "ok": result.ok,
        "checked": result.checked,
        "first_bad_seq": result.first_bad_seq,
        "gap_at": result.gap_at,
    }


@router.get("/internal/audit/sessions/{session_id}/events")
async def audit_events(
    session_id: UUID, request: Request, _role: str = role("compliance")
) -> list[dict[str, Any]]:
    """Each event's non-personal columns and header, in chain order; payloads stay encrypted."""
    return await runtime(request).audit_headers(session_id)


@router.post("/internal/kill-switches", status_code=201)
async def kill_switch(
    body: KillSwitchRequest, request: Request, actor: str = role("ops")
) -> dict[str, Any]:
    switch_id = await runtime(request).kill_switch(
        body.kind, body.target, body.active, body.reason_code, actor
    )
    return {"id": str(switch_id), "kind": body.kind, "target": body.target, "active": body.active}
