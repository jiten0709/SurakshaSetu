"""scripts/omniroute_seed.py against a fake admin API: the app key goes into the import only, drift
is reported by setting path, and no key or password value ever reaches the log."""

import copy
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


# --- the live overlay (Step 1.5b, design E) -------------------------------------------------------
LIVE: dict[str, Any] = SEED_SCRIPT.load_seed(live=True)
STUB_ID, LIVE_ID = "0199a900-0000-7000-8000-000000000001", "0199a900-0000-7000-8000-000000000002"


def test_the_live_seed_is_the_stub_seed_plus_the_overlay() -> None:
    connections = {c["id"]: c for c in LIVE["import"]["providerConnections"]}
    targets = {c["name"]: c["models"] for c in LIVE["import"]["combos"]}

    assert set(connections) == {STUB_ID, LIVE_ID}
    assert connections[LIVE_ID]["provider"] == "ollama-local"
    assert targets["gen-converse"] == ["vllm/stub-gen"]  # the gen routes stay on the stubs (O4)
    assert targets["guard-input"] == ["ollama/__UNSET__"]
    assert targets["verify-claims"] == ["ollama-local/__UNSET__"]
    assert LIVE["appKeyPolicy"]["allowedConnections"] == [STUB_ID, LIVE_ID]
    # Everything else is the stub seed's, and the stub seed itself is untouched by the merge.
    assert {k: v for k, v in LIVE.items() if k not in ("import", "appKeyPolicy")} == {
        k: v for k, v in SEED.items() if k not in ("import", "appKeyPolicy")
    }
    assert [c["id"] for c in SEED_SCRIPT.load_seed()["import"]["providerConnections"]] == [STUB_ID]


@pytest.mark.parametrize(
    ("url", "live", "refused"),
    [
        ("http://127.0.0.1:20130/v1", False, False),
        ("http://127.0.0.1:20199/v1", False, False),  # a throwaway probe instance
        ("http://127.0.0.1:20131/v1", False, True),  # the stub seed never reaches omniroute-live
        ("http://127.0.0.1:20131/v1", True, False),
        ("http://127.0.0.1:20130/v1", True, True),  # the live seed never reaches the stub database
        ("http://omniroute:20128/v1", True, True),
    ],
)
def test_each_seed_goes_only_to_its_own_instance(url: str, live: bool, refused: bool) -> None:
    assert (SEED_SCRIPT.check_target(url, live) is not None) is refused


def test_a_changed_connection_base_url_is_drift() -> None:
    sent: list[httpx.Request] = []
    moved = copy.deepcopy(SEED)
    moved["import"]["providerConnections"][0]["providerSpecificData"]["baseUrl"] = "http://x:1/v1"

    with fake_gateway(sent, drifted=False) as client:
        drift = SEED_SCRIPT.verify(client, moved)

    assert drift == [
        f"connections.{STUB_ID}.baseUrl: expected 'http://x:1/v1', got 'http://stubs:8090/v1'"
    ]


@pytest.mark.parametrize(
    ("argv", "url", "said"),
    [
        (["apply", "--live"], "http://127.0.0.1:20131/v1", "live model unset (C5)"),
        (["apply", "--live"], "http://127.0.0.1:20130/v1", "writes only to omniroute-live"),
        (["verify"], "http://127.0.0.1:20131/v1", "never goes to omniroute-live"),
        (["apply", "--force"], "http://127.0.0.1:20130/v1", "usage:"),
    ],
)
@pytest.mark.usefixtures("restore_logging")
def test_main_refuses_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    url: str,
    said: str,
) -> None:
    def no_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("no admin call may be made")

    monkeypatch.setattr(SEED_SCRIPT, "admin_client", no_network)
    monkeypatch.setattr("sys.argv", ["omniroute_seed.py", *argv])
    monkeypatch.setenv("SS_GATEWAY_BASE_URL", url)
    monkeypatch.setenv("SS_GATEWAY_API_KEY", SENTINEL_KEY)

    assert SEED_SCRIPT.main() == 2
    out = capsys.readouterr().out
    assert said in out and SENTINEL_KEY not in out
    if argv[0] == "apply" and url.endswith(":20131/v1"):
        assert "apply: live seed, gateway port 20131" in out  # the line naming seed and port
