"""Deterministic OpenAI-compatible stand-in for the chat model routes.

Until Step 9 puts the gateway in front, the combo -> model mapping lives here: `model` may be a
combo (route) name or a stub id, and the response's `model` names the stub that served it. Tests
and golden conversations script replies per (session, route) through /__script; the session is
the request's `user` field. Unscripted replies are fixed functions of the request.
"""

import hashlib
import json
import re
from collections import deque
from typing import Any

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter()

# Combo -> the stub that plays it. A bare stub id plays its first combo here.
COMBOS = {
    "guard-input": "stub-guard",
    "nlu-extract": "stub-nlu",
    "gen-converse": "stub-gen",
    "gen-recommend": "stub-gen",
    "verify-claims": "stub-verify",
    "summarise": "stub-nlu",
}
STUB_DEFAULT_COMBO = {stub: combo for combo, stub in reversed(COMBOS.items())}

# ponytail: phrase lists, not a classifier; Step 10 owns the real rails and their tests.
OVERRIDE_PHRASES = (
    "ignore previous instructions",
    "ignore all previous",
    "ignore the above",
    "disregard your instructions",
    "forget your instructions",
    "you are now",
    "system prompt",
    "developer mode",
    "पिछले निर्देश",
    "pichle nirdesh",
    "sab bhool jao",
    "instructions bhool",
)
SELF_HARM_PHRASES = (
    "kill myself",
    "suicide",
    "end my life",
    "self-harm",
    "self harm",
    "want to die",
    "hurt myself",
    "आत्महत्या",
    "मरना चाहता",
    "मरना चाहती",
    "marna chahta",
    "marna chahti",
    "khudkushi",
)
FRIENDLY = "Thanks, that helps me understand what you are looking for."
SUMMARY = "The customer is exploring life cover and has answered the questions asked so far."
TURN_ANALYSIS = {"intents": [], "slots": [], "side_query": None, "language": "en"}
HANDLE = re.compile(r'(?:\[|id=")([RE]\d+)(?:\]|")')
MAX_CITED_EVIDENCE = 3


class Scripted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str | dict[str, Any] | list[Any] = ""
    status: int = 200
    headers: dict[str, str] = {}


class Script(BaseModel):
    session_id: str
    route: str
    responses: list[str | Scripted]


class Message(BaseModel):
    role: str
    content: str | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message]
    user: str | None = None
    stream: bool = False


_queues: dict[tuple[str, str], deque[Scripted]] = {}


@router.post("/__script")
async def queue_script(script: Script) -> dict[str, int]:
    queue = _queues.setdefault((script.session_id, script.route), deque())
    queue.extend(Scripted(content=r) if isinstance(r, str) else r for r in script.responses)
    return {"queued": len(queue)}


@router.delete("/__script/{session_id}", status_code=204)
async def clear_script(session_id: str) -> None:
    for key in [k for k in _queues if k[0] == session_id]:
        del _queues[key]


@router.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest) -> Response:
    if request.stream:
        return _error(400, "streaming is not supported", "invalid_request_error")
    if request.model in COMBOS:
        combo, stub = request.model, COMBOS[request.model]
    elif request.model in STUB_DEFAULT_COMBO:
        combo, stub = STUB_DEFAULT_COMBO[request.model], request.model
    else:
        return _error(404, f"unknown model {request.model}", "model_not_found")

    queue = _queues.get((request.user, request.model)) if request.user else None
    reply = queue.popleft() if queue else Scripted(content=_default(combo, request.messages))
    content = reply.content if isinstance(reply.content, str) else json.dumps(reply.content)
    if reply.status >= 400:
        return _error(reply.status, content or "scripted failure", "scripted", reply.headers)

    prompt_words = sum(len((m.content or "").split()) for m in request.messages)
    digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
    body = {
        "id": f"chatcmpl-{digest[:24]}",
        "object": "chat.completion",
        "created": 0,
        "model": stub,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_words,
            "completion_tokens": len(content.split()),
            "total_tokens": prompt_words + len(content.split()),
        },
    }
    return JSONResponse(body, status_code=reply.status, headers=reply.headers)


def _default(combo: str, messages: list[Message]) -> str:
    text = next((m.content or "" for m in reversed(messages) if m.role == "user"), "")
    if combo == "guard-input":
        folded = " ".join(text.casefold().split())
        injection = any(p in folded for p in OVERRIDE_PHRASES)
        self_harm = any(p in folded for p in SELF_HARM_PHRASES)
        return json.dumps(
            {
                "injection_score": 0.99 if injection else 0.01,
                "safety": "unsafe S11" if self_harm else "safe",
            }
        )
    if combo == "nlu-extract":
        return json.dumps(TURN_ANALYSIS)
    if combo == "summarise":
        return SUMMARY
    if combo == "verify-claims":
        return json.dumps({"verdict": "not_entailed" if "UNSUPPORTED" in text else "entailed"})
    if combo == "gen-recommend":
        return _narrative(" ".join(m.content or "" for m in messages)) or FRIENDLY
    return FRIENDLY


def _narrative(prompt: str) -> str:
    """One short paragraph per engine result [R#], citing the evidence [E#]; no numbers."""
    handles = sorted(set(HANDLE.findall(prompt)), key=lambda h: (h[0], int(h[1:])))
    evidence = [f"[{h}]" for h in handles if h[0] == "E"][:MAX_CITED_EVIDENCE]
    cited = (
        f" Its main terms and exclusions are in the cited documents {' '.join(evidence)}."
        if evidence
        else ""
    )
    return "\n\n".join(
        f"Option [{r}] fits the protection need you described and stays within the budget you "
        f"shared.{cited} Please read its disclosures before you decide."
        for r in handles
        if r[0] == "R"
    )


def _error(
    status: int, message: str, code: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {"error": {"message": message, "type": "invalid_request_error", "code": code}}
    return JSONResponse(body, status_code=status, headers=headers)
