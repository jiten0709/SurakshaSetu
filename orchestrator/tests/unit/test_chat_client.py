"""The demo chat client (scripts/chat.py) against a MockTransport: no network."""

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx

SCRIPT = Path(__file__).parents[2] / "scripts" / "chat.py"
FORM = {
    "type": "CONSENT_SUBMIT",
    "notice_version": "N-1",
    "notice_sha256": "a" * 64,
    "language": "en-IN",
    "purposes": [
        {"id": "P1", "label": "Needs", "required": True},
        {"id": "P2", "label": "Advisor", "required": False},
        {"id": "P3", "label": "Marketing", "required": False},
    ],
    "adult": {"label": "I am 18 or older"},
    "submit": "Submit",
}


def load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("chat", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_consent_form_becomes_a_consent_submit_payload() -> None:
    answers = iter(["y", "n", "no", "Y"])
    payload = load().consent_payload(FORM, lambda _: next(answers))
    assert payload == {
        "purposes": {"P1": True, "P2": False, "P3": False},
        "age_18_plus": True,
        "notice_version": "N-1",
        "notice_sha256": "a" * 64,
    }


def test_a_quick_reply_sends_its_own_action_with_a_fresh_key() -> None:
    intent = {"type": "INTENT", "payload": {"intent": "new_purchase"}}
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if request.url.path == "/v1/sessions":
            return httpx.Response(201, json={"session_id": "s1", "session_token": "tok"})
        body: dict[str, Any] = {
            "state": "S0",
            "message": {
                "text": "Hello",
                "sources": [],
                "quick_replies": [{"label": "Buy", "action": intent}],
                "form": FORM,
            },
        }
        return httpx.Response(200, json=body)

    answers = iter(["1", "/quit"])
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler)) as client:
        load().chat(client, "en-IN", lambda _: next(answers))
    turns = [r for r in sent if r.url.path == "/v1/sessions/s1/turns"]
    assert [json.loads(r.content) for r in turns] == [
        {"action": {"type": "START", "payload": {}}},
        {"action": intent},
    ]
    assert all(r.headers["Authorization"] == "Bearer tok" for r in turns)
    assert len({r.headers["Idempotency-Key"] for r in turns}) == 2
