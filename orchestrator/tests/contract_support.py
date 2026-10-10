"""The Conversation API contract (contracts/openapi/conversation-api.v1.yaml, Step 26) as a
validator, shared by the unit tests and the golden Play. Every finding is "<json path> (<keyword>)":
the place and the rule, never the value (a turn body is customer text)."""

import json
import re
from functools import cache
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry
from referencing.jsonschema import DRAFT202012

SPEC_PATH = Path(__file__).resolve().parents[2] / "contracts/openapi/conversation-api.v1.yaml"
_URN = "urn:surakshasetu:conversation-api"


@cache
def spec() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load(SPEC_PATH.read_text(encoding="utf-8"))
    return loaded


@cache
def _validator(pointer: str) -> Draft202012Validator:
    registry = Registry().with_resource(_URN, DRAFT202012.create_resource(spec()))
    return Draft202012Validator(
        {"$ref": f"{_URN}{pointer}"},
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )


def pointer(*keys: str) -> str:
    """A JSON pointer fragment into the spec, e.g. pointer("components", "schemas", "Part")."""
    escaped = (k.replace("~", "~0").replace("/", "~1") for k in keys)
    return "#/" + "/".join(quote(k, safe="~") for k in escaped)


def schema(name: str) -> str:
    return pointer("components", "schemas", name)


def errors(ref: str, instance: Any) -> list[str]:
    """`ref` is a pointer into the spec (schema(name) for a component)."""
    found = [line for e in _validator(ref).iter_errors(instance) for line in _report(e)]
    return sorted(set(found))


def _report(error: ValidationError) -> list[str]:
    path, keyword, at = error.json_path, str(error.validator), error.schema
    instance = error.instance
    if keyword == "oneOf" and isinstance(at, dict) and "discriminator" in at:
        # Follow the discriminator into the branch it names, so the path ends at the bad field.
        name = at["discriminator"]["propertyName"]
        target = (
            at["discriminator"]["mapping"].get(instance.get(name))
            if isinstance(instance, dict)
            else None
        )
        if target is None:
            return [f"{path}.{name} (discriminator)"]
        branch = [b.get("$ref") for b in at["oneOf"]].index(target)
        return [
            line
            for c in error.context or []
            if c.relative_schema_path[0] == branch
            for line in _report(c)
        ]
    if keyword == "required" and isinstance(instance, dict):
        return [f"{path}.{p} (required)" for p in error.validator_value if p not in instance]
    if keyword == "additionalProperties" and isinstance(instance, dict) and isinstance(at, dict):
        known = at.get("properties", {})
        return [f"{path}.{k} (additionalProperties)" for k in instance if k not in known]
    return [f"{path} ({keyword})"]


def _operation(method: str, path: str) -> tuple[str, dict[str, Any]] | None:
    for template, item in spec()["paths"].items():
        pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template))
        if re.fullmatch(pattern, path) and method.lower() in item:
            return template, item[method.lower()]
    return None


def response_errors(
    method: str, path: str, status: int, content_type: str, body: bytes
) -> list[str]:
    """One HTTP response against the operation it answers: the status must be documented, then
    the body must match the schema for its content type. A route or method the spec does not
    have (404, 405) answers with a plain Problem."""
    media = content_type.split(";")[0].strip()
    found = _operation(method, path)
    if found is None:
        if media != "application/problem+json":
            return [f"{method} <undocumented route>: {status} is not a problem"]
        return errors(schema("Problem"), json.loads(body))
    template, operation = found
    name = f"{method.upper()} {template}"
    response = operation["responses"].get(str(status))
    if response is None:
        return [f"{name}: status {status} not documented"]
    keys = ["paths", template, method.lower(), "responses", str(status)]
    if "$ref" in response:
        keys = ["components", "responses", response["$ref"].rsplit("/", 1)[1]]
        response = spec()["components"]["responses"][keys[-1]]
    if media not in response.get("content", {}):
        return [f"{name}: {status} {media} not documented"]
    if media == "text/event-stream":
        return []  # the stream's events go through event_errors
    found = errors(pointer(*keys, "content", media, "schema"), json.loads(body))
    return [f"{name}: {e}" for e in found]


def event_errors(event: str, data: Any) -> list[str]:
    """One server-sent event's data, through the stream's x-events."""
    stream = spec()["paths"]["/v1/sessions/{session_id}/events"]["get"]["responses"]["200"]
    events = stream["content"]["text/event-stream"]["x-events"]
    if event not in events:
        return [f"event {event}: not documented"]
    return [f"event {event}: {e}" for e in errors(events[event]["$ref"], data)]
