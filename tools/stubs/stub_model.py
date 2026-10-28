"""Deterministic OpenAI-compatible stand-in for the chat model routes.

`model` is a combo (route) name when called directly, or the combo's own stub id when OmniRoute
calls it (infra/omniroute/seed.json targets each combo at its stub id). Each stub plays exactly
one combo, so the stub always knows its role, and the response's `model` names the stub. Tests and
golden conversations script replies per (session, route) through /__script; the session is the
request's `user` field. Unscripted replies are fixed functions of the request.
"""

import hashlib
import json
import re
from collections import deque
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

router = APIRouter()

# Combo -> the stub that plays it, one to one: OmniRoute sends the stub id, so it must say which
# combo is calling.
COMBOS = {
    "guard-input": "stub-guard",
    "nlu-extract": "stub-nlu",
    "gen-converse": "stub-gen",
    "gen-recommend": "stub-recommend",
    "verify-claims": "stub-verify",
    "summarise": "stub-summarise",
}
STUB_COMBO = {stub: combo for combo, stub in COMBOS.items()}

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
# gen-converse's lead sentence before each S1/S2 question, keyed by words of the NEXT_SLOT question
# the L1 carries: one line per question, so no two replies in a row open the same way. Each must
# pass the output rails as plain text: no digit, number word, product/regulatory/tax term, promise.
BRIDGES = (
    ("how old are you", "Let's start with a little about you."),
    ("what is your gender", "A quick detail about you next."),
    ("resident of india", "Next, a quick check on where you're based."),
    ("pincode", "Now a quick check on your area."),
    ("tobacco", "This next question matters, so an honest answer really helps."),
    ("occupation", "Now a little about the work you do."),
    ("health screening", "I appreciate you bearing with these checks."),
    ("for your own life", "Let's make sure I understand who the cover is for."),
    ("how old is the person", "Good to know who you'd like to protect."),
    ("how are you related", "And a little about how you're connected."),
    ("business purpose", "And a quick check on what the cover is for."),
    ("what should this cover do", "Let's talk about what matters most to you."),
    ("annual income", "Next, a little about what you earn."),
    ("salaried, self-employed", "And a little about how you earn your living."),
    ("depends on you", "Now a little about the people who count on you."),
    ("any loans", "Next, anything you're still paying off."),
    ("life cover do you already have", "Let's see what you already have in place."),
    ("employer give you", "And anything your workplace provides."),
    ("in premiums", "And what you already spend on your current policies."),
    ("set aside savings", "Almost there: anything you've put aside for your family."),
    ("premium budget", "And what would feel comfortable for you to spend."),
)
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
# The last request body each session sent, exactly as received: tests read it to prove the gateway
# forwards what the adapter sent. one body per session until DELETE /__script clears it.
_last: dict[str, Any] = {}


@router.post("/__script")
async def queue_script(script: Script) -> dict[str, int]:
    queue = _queues.setdefault((script.session_id, script.route), deque())
    queue.extend(Scripted(content=r) if isinstance(r, str) else r for r in script.responses)
    return {"queued": len(queue)}


@router.delete("/__script/{session_id}", status_code=204)
async def clear_script(session_id: str) -> None:
    for key in [k for k in _queues if k[0] == session_id]:
        del _queues[key]
    _last.pop(session_id, None)


@router.get("/__last/{session_id}")
async def last_request(session_id: str) -> Response:
    if session_id not in _last:
        return _error(404, "no request from this session", "not_found")
    return JSONResponse(_last[session_id])


@router.post("/v1/chat/completions")
async def chat_completions(request: ChatRequest, raw: Request) -> Response:
    if request.stream:
        return _error(400, "streaming is not supported", "invalid_request_error")
    if request.model in COMBOS:
        combo, stub = request.model, COMBOS[request.model]
    elif request.model in STUB_COMBO:
        combo, stub = STUB_COMBO[request.model], request.model
    else:
        return _error(404, f"unknown model {request.model}", "model_not_found")

    if request.user:
        _last[request.user] = await raw.json()
    queue = _queues.get((request.user, combo)) if request.user else None
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
        # The last message, whatever its role: in output mode (Step 14) it is the assistant's reply.
        folded = " ".join((messages[-1].content or "" if messages else "").casefold().split())
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
    system = next((m.content or "" for m in messages if m.role == "system"), "")
    next_slot = system.rpartition("NEXT_SLOT:")[2].casefold()
    return next((line for key, line in BRIDGES if key in next_slot), FRIENDLY)


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
