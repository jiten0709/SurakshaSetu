"""Deterministic OpenAI-compatible stand-in for the chat model routes.

`model` is a combo (route) name when called directly, or the combo's own stub id when OmniRoute
calls it (infra/omniroute/seed.json targets each combo at its stub id). Each stub plays exactly
one combo, so the stub always knows its role, and the response's `model` names the stub. Tests and
golden conversations script replies per (session, route) through /__script; the session is the
request's `user` field. Unscripted replies are fixed functions of the request: gen-recommend
narrates each engine fact it was given (S3) or answers from the first evidence block (a side query;
enhancement Step 1.1).
"""

import hashlib
import html
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
# the L1 carries: one line per question, so no two replies in a row open the same way, and no two
# lines share their first two words (fix 2: "a little about" six times read as a script). Each must
# pass the output rails as plain text: no digit, number word, product/regulatory/tax term, promise.
BRIDGES = (
    ("how old are you", "Let's start with a little about you."),
    ("what is your gender", "Just a detail about you next."),
    ("resident of india", "Next, where you're based."),
    ("pincode", "Now to your area."),
    ("tobacco", "A quick lifestyle question next."),
    ("occupation", "Now, about the work you do."),
    ("serious illness", "I appreciate you bearing with these checks."),
    ("for your own life", "Let's make sure I understand who the cover is for."),
    ("how old is the person", "Good to know who you'd like to protect."),
    ("how are you related", "And how you're connected."),
    ("business purpose", "And what the cover is for."),
    ("what should this cover do", "Let's talk about what matters most to you."),
    ("annual income", "Next, what you earn."),
    ("salaried, self-employed", "Also, how you earn your living."),
    ("depends on you", "Now the people who count on you."),
    ("any loans", "Next, anything you're still paying off."),
    ("life cover do you already have", "Let's see what you already have in place."),
    ("employer give you", "And anything your workplace provides."),
    ("in premiums", "Then, what you already spend on your current policies."),
    ("set aside savings", "Almost there: anything you've put aside for your family."),
    ("premium budget", "Last, what would feel comfortable for you to spend."),
)
SUMMARY = "The customer is exploring life cover and has answered the questions asked so far."
TURN_ANALYSIS = {"intents": [], "slots": [], "side_query": None, "language": "en"}
MAX_CITED_EVIDENCE = 3
# A phrase only l1/side-query.txt carries: the envelope's system message is L0 + one L1.
SIDE_QUERY_L1 = "SIDE QUERY: the customer asked a question outside the pending step."
ENGINE = re.compile(r'<engine id="(R\d+)"[^>]*>(.*?)</engine>', re.S)
EVIDENCE = re.compile(r'<evidence id="(E\d+)"[^>]*>(.*?)</evidence>', re.S)
SENTENCE_END = re.compile(r"(?<=[.!?।])\s+|\n+")  # the output rails' split (rails/output.py)
KINDS = {"TERM": "term", "TERM_ROP": "term with return of premium"}
# The ranker's sizing codes, in words with no number (GR-NUMBER) and no ordinal.
SIZING = {
    "RANK-SA-CAPPED": "its cover is capped below your need",
    "RANK-SA-MIN": "its cover is raised to the plan's minimum",
    "RANK-TERM-CAPPED": "its term ends before your need does",
    "RANK-TERM-MIN": "its term is raised to the plan's minimum",
}


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
    system = next((m.content or "" for m in messages if m.role == "system"), "")
    if combo == "gen-recommend":
        reply = _answer(text) if SIDE_QUERY_L1 in system else _narrative(text)
        return reply or FRIENDLY
    next_slot = system.rpartition("NEXT_SLOT:")[2].casefold()
    return next((line for key, line in BRIDGES if key in next_slot), FRIENDLY)


def _narrative(prompt: str) -> str:
    """One sentence per engine fact, from its own content and cited at its end, then one sentence
    citing the evidence (the composer lists sources only for cited evidence). No numbers."""
    lines = [_fact(h, json.loads(html.unescape(body))) for h, body in ENGINE.findall(prompt)]
    evidence = [f"[{h}]" for h, _ in EVIDENCE.findall(prompt)][:MAX_CITED_EVIDENCE]
    if lines and evidence:
        cited = " ".join(evidence)
        lines.append(f"Their main terms and exclusions are in the cited documents {cited}.")
    return "\n\n".join(lines)


def _fact(handle: str, fact: dict[str, Any]) -> str:
    if "uin" not in fact:  # the needs assessment: a budget claim only when the engine says green
        budget = ", within the budget you shared" if fact.get("affordability") == "green" else ""
        return f"These options are matched to the needs you confirmed{budget} [{handle}]."
    codes = fact.get("reason_codes") or []
    fit = next((c.removeprefix("RANK-FIT-") for c in codes if c.startswith("RANK-FIT-")), None)
    kind = KINDS.get(fit, fit.lower().replace("_", " ")) if fit else "protection"
    clauses = [f"The plan {fact['uin']} fits the {kind} cover your needs call for"]
    clauses += [SIZING[c] for c in codes if c in SIZING]
    if riders := fact.get("rider_uins") or []:
        clauses.append(f"with rider{'s' if len(riders) > 1 else ''} {', '.join(riders)}")
    return f"{', '.join(clauses)} [{handle}]."


def _answer(prompt: str) -> str:
    """A side question: the first evidence block's first sentence (as the rails split it), cited."""
    first = EVIDENCE.search(prompt)
    if first is None:
        return ""
    sentence = SENTENCE_END.split(" ".join(html.unescape(first.group(2)).split()), maxsplit=1)[0]
    return f"{sentence} [{first.group(1)}]"


def _error(
    status: int, message: str, code: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = {"error": {"message": message, "type": "invalid_request_error", "code": code}}
    return JSONResponse(body, status_code=status, headers=headers)
