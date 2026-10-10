"""The Conversation API contract (Step 26): the spec lints; the code's action types, part
namespaces, events and states are exactly the spec's (the drift check); the validator names paths,
never values; and real released bodies, built by the state nodes with the test_s0/test_s3 fakes,
validate. The golden Play validates every response and event of every conversation against the
stack (`make e2e-scripted`, `make eval`); test_api.py validates every problem response."""

import ast
import re
import typing
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx
import pytest
from contract_support import errors, event_errors, response_errors, schema, spec
from fastapi.testclient import TestClient
from openapi_spec_validator import validate
from runtime_support import TERM, FakeGate, domain, domain_handler, problem, settings
from test_runtime_nodes import FakeConn, Recorder, run
from test_s0 import s0_turn
from test_s3 import (  # noqa: F401 (autouse fixtures: the S3 store, audit and journey fakes)
    ack_action,
    audited,
    entered,
    follow,
    journey,
    presented,
    s3_turn,
    stored,
)

import surakshasetu
from surakshasetu.api.app import create_app
from surakshasetu.api.routes import CreateSession
from surakshasetu.compose.bundle import load_bundle
from surakshasetu.config import Settings
from surakshasetu.fsm.states import FsmState
from surakshasetu.graph import nodes
from surakshasetu.graph.nodes import Turn
from surakshasetu.graph.runtime import Runtime
from surakshasetu.store import conv as store

SRC = Path(surakshasetu.__file__).parent
CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SCHEMAS = spec()["components"]["schemas"]


def sources(*dirs: str) -> dict[str, str]:
    return {str(p): p.read_text(encoding="utf-8") for d in dirs for p in (SRC / d).rglob("*.py")}


# --- the action types the code reads or offers ----------------------------------------------------
def _reads_type(node: ast.expr) -> bool:
    """`kind` / `action_type`, `<x>.get("type")` or `<x>["type"]`: an action's type."""
    if isinstance(node, ast.Name):
        return node.id in ("kind", "action_type")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        first = node.args[0] if node.args else None
        return node.func.attr == "get" and isinstance(first, ast.Constant) and first.value == "type"
    if isinstance(node, ast.Subscript):
        return isinstance(node.slice, ast.Constant) and node.slice.value == "type"
    return False


def action_types(code: Iterable[str]) -> set[str]:
    """Every action type compared against (`kind == "SAVE"`, `.get("type") == ERASE`) or offered
    (`quick_reply(label, "APPLY", ...)`, `_choice(turn, "ELECT", ...)`), with module constants
    resolved."""
    trees = [ast.parse(c) for c in code]
    constants = {
        target.id: node.value.value
        for tree in trees
        for node in tree.body
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def value(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, str) else None
        if isinstance(node, ast.Name):
            return constants.get(node.id)
        if isinstance(node, ast.Attribute):
            return constants.get(node.attr)
        return None

    found: set[str] = set()
    for tree in trees:
        for node in ast.walk(tree):
            candidates: list[ast.expr] = []
            if isinstance(node, ast.Compare) and _reads_type(node.left):
                for c in node.comparators:
                    candidates += c.elts if isinstance(c, ast.Tuple | ast.List | ast.Set) else [c]
            elif isinstance(node, ast.Call) and len(node.args) >= 2:
                called = node.func
                name = called.id if isinstance(called, ast.Name) else getattr(called, "attr", "")
                if name in ("quick_reply", "_choice"):
                    candidates = [node.args[1]]
            found |= {v for v in map(value, candidates) if v is not None and CODE.match(v)}
    return found


def spec_action_types() -> set[str]:
    return set(SCHEMAS["Action"]["discriminator"]["mapping"])


def test_the_spec_lints() -> None:
    validate(spec())


def test_every_action_type_in_the_code_is_in_the_spec_and_no_other() -> None:
    found = action_types(sources("graph").values())
    assert found - spec_action_types() == set(), "action types the spec lacks"
    assert spec_action_types() - found == set(), "spec action types no code reads or offers"


def test_the_scan_catches_a_new_action_type() -> None:
    planted = 'quick_reply(label, "NEW_OFFER", {})\nif kind in ("NEW_READ", "lower"):\n    pass'
    assert action_types([planted]) - spec_action_types() == {"NEW_OFFER", "NEW_READ"}


def test_each_action_type_maps_to_a_schema_that_takes_it() -> None:
    for kind, target in SCHEMAS["Action"]["discriminator"]["mapping"].items():
        found = errors(target, {"type": kind, "payload": {}})
        assert [f for f in found if f.startswith("$.type")] == [], kind


# --- part ids -------------------------------------------------------------------------------------
def _literal(node: ast.expr) -> str | None:
    """A string, an f-string's leading text, or (`i if ":" in i else f"template:{i}"`) its else."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr) and node.values and isinstance(node.values[0], ast.Constant):
        return str(node.values[0].value)
    if isinstance(node, ast.IfExp):
        return _literal(node.orelse)
    return None


def part_ids(code: Iterable[str]) -> tuple[set[str], set[str]]:
    """(namespaces, bare ids) of every (id, text) pair whose id is a literal."""
    namespaces, bare = set(), set()
    for c in code:
        for node in ast.walk(ast.parse(c)):
            if isinstance(node, ast.Tuple) and len(node.elts) == 2:
                literal = _literal(node.elts[0])
                if literal is None:
                    continue
                if m := re.match(r"^([a-z_]+):", literal):
                    namespaces.add(m.group(1))
                else:
                    bare.add(literal)
    return namespaces, bare


def branches() -> dict[str, dict[str, Any]]:
    return {b["title"]: b for b in SCHEMAS["PartId"]["anyOf"]}


def test_every_part_namespace_in_the_code_is_in_the_spec_and_no_other() -> None:
    found, _ = part_ids(sources("graph", "compose").values())
    assert found == set(branches()) - {"composer"}


def test_the_composers_own_part_ids_are_the_specs() -> None:
    _, bare = part_ids([(SRC / "compose" / "composer.py").read_text(encoding="utf-8")])
    assert bare == set(branches()["composer"]["enum"])


def test_every_template_of_the_active_bundle_is_a_valid_part_id() -> None:
    bundle = load_bundle(Settings(_env_file=None).prompt_bundle, env="dev")
    names: set[str] = set()
    for templates in bundle.templates.values():
        names |= set(type(templates.scripts).model_fields)
        names |= set(templates.slots)
        names |= set(type(templates.recommendation).model_fields)
    bad = [n for n in sorted(names) if errors(schema("PartId"), f"template:{n}")]
    assert bad == []


# --- events, states and the session request -------------------------------------------------------
def test_the_events_and_statuses_published_are_the_specs() -> None:
    code = (SRC / "graph" / "nodes.py").read_text(encoding="utf-8")
    stream = spec()["paths"]["/v1/sessions/{session_id}/events"]["get"]["responses"]["200"]
    assert set(re.findall(r'_publish\(turn, "([a-z.]+)"', code)) == set(
        stream["content"]["text/event-stream"]["x-events"]
    )
    statuses = SCHEMAS["TurnStatusEvent"]["properties"]["status"]["enum"]
    assert set(re.findall(r'_status\(turn, "([a-z]+)"\)', code)) == set(statuses)


def test_the_states_and_session_choices_are_the_specs() -> None:
    assert SCHEMAS["FsmState"]["enum"] == [s.value for s in FsmState]
    request = SCHEMAS["CreateSessionRequest"]["properties"]
    channel = CreateSession.model_fields["channel"].annotation
    locale = CreateSession.model_fields["locale"].annotation
    assert request["channel"]["enum"] == list(typing.get_args(channel))
    assert SCHEMAS["Locale"]["enum"] == list(typing.get_args(locale))


# --- the validator --------------------------------------------------------------------------------
SENTINEL = "my PAN is ABCDE1234F"


def test_findings_name_the_path_and_the_rule_never_the_value() -> None:
    body = {
        "turn_id": SENTINEL,
        "state": SENTINEL,
        "message": {
            "text": "x",
            "parts": [{"id": SENTINEL, "text": SENTINEL}],
            "citations": [],
            "sources": [],
            "disclosures": [],
            "cta": None,
            "form": None,
            "quick_replies": [
                {"label": "x", "action": {"type": "APPLY", "payload": {"uin": SENTINEL}}},
                {"label": "x", "action": {"type": SENTINEL, "payload": {}}},
            ],
            "extra": 1,
        },
    }
    found = errors(schema("TurnResponse"), body)
    assert found == [
        "$.documents (required)",
        "$.message.extra (additionalProperties)",
        "$.message.parts[0].id (anyOf)",
        "$.message.quick_replies[0].action.payload.uin (pattern)",
        "$.message.quick_replies[1].action.type (discriminator)",
        "$.state (enum)",
        "$.turn_id (format)",
    ]
    assert not any("ABCDE1234F" in f for f in found)


def test_a_status_the_operation_does_not_document_is_a_finding() -> None:
    problem = b'{"type":"about:blank","title":"Conflict","status":409,"code":"SESSION_BUSY"}'
    path = "/v1/sessions/0199a1b2-0000-7000-8000-00000000c0de"
    assert response_errors("POST", f"{path}/turns", 409, "application/problem+json", problem) == []
    assert response_errors("GET", f"{path}/events", 409, "application/problem+json", problem) == [
        "GET /v1/sessions/{session_id}/events: status 409 not documented"
    ]
    wrong = problem.replace(b"SESSION_BUSY", b"RATE_LIMITED")
    assert response_errors("DELETE", path, 409, "application/problem+json", wrong) == [
        "DELETE /v1/sessions/{session_id}: $.code (const)"
    ]
    assert event_errors("turn.progress", {}) == ["event turn.progress: not documented"]


# --- real bodies, as the state nodes build them ---------------------------------------------------
def released(t: Turn) -> dict[str, Any]:
    """The turn body and every event published, validated."""
    findings = errors(schema("TurnResponse"), nodes.response_body(t))
    findings += [e for event, data in t.gate.published for e in event_errors(event, data)]  # type: ignore[attr-defined]
    assert findings == []
    return nodes.response_body(t)


@pytest.mark.asyncio
async def test_the_greeting_matches_the_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    Recorder(monkeypatch)
    t = s0_turn(action={"type": "START", "payload": {}}, prompt=None)
    await run(t)

    body = released(t)
    assert body["message"]["form"] is not None and body["message"]["quick_replies"]


@pytest.mark.asyncio
async def test_the_recommendation_and_the_choices_after_it_match_the_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recommended = released(await entered(s3_turn()))
    assert recommended["message"]["disclosures"] and recommended["documents"]
    assert recommended["message"]["citations"]

    held = await presented(monkeypatch)
    cheaper = await follow(held, action={"type": "CHEAPER", "payload": {"uin": TERM}})
    assert {q["action"]["type"] for q in released(cheaper)["message"]["quick_replies"]} >= {"APPLY"}

    chosen = await follow(held, action={"type": "APPLY", "payload": {"uin": TERM}})
    offered = {q["action"]["type"] for q in released(chosen)["message"]["quick_replies"]}
    assert "DISCLOSURE_ACK" in offered

    handed = await follow(chosen.next, action=ack_action(chosen))  # type: ignore[arg-type]
    assert released(handed)["state"] == "HANDOFF"


# --- session creation, and a method the route does not have ---------------------------------------
class Pool:
    def getconn(self) -> FakeConn:
        return FakeConn()

    def putconn(self, conn: FakeConn) -> None:
        pass


def versions_down(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("down")


def versions_refused(request: httpx.Request) -> httpx.Response:
    return problem(404, "NOT_FOUND")


@pytest.mark.parametrize(
    ("handler", "body", "status"),
    [
        (domain_handler, {"channel": "web", "locale": "en-IN"}, 201),
        (domain_handler, {"channel": "sms", "locale": "en-IN"}, 400),
        (versions_refused, {"channel": "app", "locale": "hi-IN"}, 502),
        (versions_down, {"channel": "web", "locale": "en-IN"}, 503),
    ],
)
def test_session_creation_matches_the_contract(
    monkeypatch: pytest.MonkeyPatch, handler: Any, body: dict[str, str], status: int
) -> None:
    monkeypatch.setattr(store, "active_snapshots", lambda conn: {})
    monkeypatch.setattr(store, "insert_session", lambda conn, **row: None)
    keys = type("Keys", (), {"create_subject_key": lambda self, ref: "key-ref"})()
    rt = Runtime(
        settings(),
        pool=Pool(),  # type: ignore[arg-type]
        erasure=None,  # type: ignore[arg-type]
        keys=keys,
        gate=FakeGate(),  # type: ignore[arg-type]
        domain=domain(handler),
        gateway=None,  # type: ignore[arg-type]
        pack=None,  # type: ignore[arg-type]
        graph=None,  # type: ignore[arg-type]
    )
    client = TestClient(create_app(settings(), runtime=rt), raise_server_exceptions=False)

    responses = [client.post("/v1/sessions", json=body), client.put("/v1/sessions", json=body)]

    assert [r.status_code for r in responses] == [status, 405]
    for r in responses:
        content_type = r.headers["content-type"]
        found = response_errors(
            r.request.method, r.request.url.path, r.status_code, content_type, r.content
        )
        assert found == []
