"""scripts/omniroute_seed.py against a fake admin API: the app key goes into the import only, drift
is reported by setting path, and no key or password value ever reaches the log."""

import importlib.util
import json
import logging
from pathlib import Path
from types import ModuleType
from typing import Any

import httpx
import pytest

SENTINEL_KEY = "sk-SENTINEL-app-key-4242"


def _load() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "omniroute_seed.py"
    spec = importlib.util.spec_from_file_location("omniroute_seed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED_SCRIPT = _load()
SEED: dict[str, Any] = SEED_SCRIPT.load_seed()


def fake_gateway(sent: list[httpx.Request], *, drifted: bool) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        path = request.url.path
        if request.method != "GET":
            return httpx.Response(200, json={})
        if path == "/api/settings/database":
            logs = SEED["database"]["logs"] | {"ringBufferSize": 1000}
            return httpx.Response(200, json={"logs": logs, "cache": SEED["database"]["cache"]})
        if path == "/api/settings":
            settings = SEED["settings"] | SEED["import"]["settings"] | {"requireLogin": True}
            return httpx.Response(200, json=settings | ({"mcpEnabled": True} if drifted else {}))
        if path.startswith("/api/keys/"):
            # The whole key record, value included: verify must only ever report policy fields.
            key = SEED["appKeyPolicy"] | {"key": SENTINEL_KEY}
            return httpx.Response(200, json=key | ({"noLog": False} if drifted else {}))
        if path == "/api/settings/feature-flags":
            flags = [
                {"key": k, "effectiveValue": v, "source": "env"}
                for k, v in SEED["featureFlags"].items()
            ]
            return httpx.Response(200, json={"flags": flags})
        if path == "/api/providers":
            return httpx.Response(200, json={"connections": SEED["import"]["providerConnections"]})
        if path == "/api/combos":
            return httpx.Response(200, json={"combos": SEED["import"]["combos"]})
        section = {
            "/api/resilience": "resilience",
            "/api/settings/memory": "memory",
            "/api/settings/compression": "compression",
        }[path]
        return httpx.Response(200, json=SEED[section])

    return httpx.Client(base_url="http://gateway.test", transport=httpx.MockTransport(handler))


def test_apply_imports_the_key_from_settings_and_sets_every_section() -> None:
    sent: list[httpx.Request] = []

    with fake_gateway(sent, drifted=False) as client:
        SEED_SCRIPT.apply(client, SEED, SENTINEL_KEY)

    imported = json.loads(sent[0].content)
    assert sent[0].url.path == "/api/settings/import-json"
    assert [k["key"] for k in imported["apiKeys"]] == [SENTINEL_KEY]
    assert "key" not in SEED["import"]["apiKeys"][0]  # the seed itself is not modified
    database = json.loads(
        next(r for r in sent if r.method == "PATCH" and "database" in str(r.url)).content
    )
    assert database["logs"]["ringBufferSize"] == 1000  # the fields the seed doesn't set are kept
    written = {(r.method, r.url.path) for r in sent}
    assert {
        ("PATCH", "/api/settings"),
        ("PATCH", "/api/resilience"),
        ("PUT", "/api/settings/memory"),
        ("PUT", "/api/settings/compression"),
        ("PATCH", f"/api/keys/{SEED['import']['apiKeys'][0]['id']}"),
    } <= written


def test_a_matching_gateway_has_no_drift() -> None:
    with fake_gateway([], drifted=False) as client:
        assert SEED_SCRIPT.verify(client, SEED) == []


def test_drift_is_reported_by_path_without_leaking_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)

    with fake_gateway([], drifted=True) as client:
        drift = SEED_SCRIPT.verify(client, SEED)

    assert drift == [
        "settings.mcpEnabled: expected False, got True",
        "appKeyPolicy.noLog: expected True, got False",
    ]
    assert SENTINEL_KEY not in caplog.text


def test_an_admin_error_names_the_call_but_not_its_body() -> None:
    client = httpx.Client(
        base_url="http://gateway.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})),
    )

    with client, pytest.raises(SEED_SCRIPT.SeedError) as failed:
        SEED_SCRIPT.apply(client, SEED, SENTINEL_KEY)

    assert str(failed.value) == "POST /api/settings/import-json -> 401"
