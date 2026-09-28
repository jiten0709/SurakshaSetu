"""TDD §1.6 as written in Git: the omniroute compose service and infra/omniroute/seed.json.

No network. tests/stack/test_omniroute_stack.py proves the running gateway matches and behaves;
infra/omniroute/HARDENING.md maps each row to the tests here and there.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

from surakshasetu.gateway import Route

INFRA = Path(__file__).parents[3] / "infra"
ORCHESTRATOR_TESTS = Path(__file__).parents[1]
SERVICE: dict[str, Any] = yaml.safe_load((INFRA / "compose.yaml").read_text())["services"][
    "omniroute"
]
ENV: dict[str, str] = SERVICE["environment"]
SEED: dict[str, Any] = json.loads((INFRA / "omniroute" / "seed.json").read_text())
CHAT_ROUTES = {r.value for r in Route} - {Route.EMBED.value, Route.RERANK.value}
KEY = SEED["appKeyPolicy"]


def test_the_image_is_one_release_pinned_by_digest() -> None:
    assert re.fullmatch(
        r"diegosouzapw/omniroute:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", SERVICE["image"]
    )


def test_the_dashboard_and_api_listen_on_loopback_only() -> None:
    assert SERVICE["ports"]
    assert all(port.startswith("127.0.0.1:") for port in SERVICE["ports"])


def test_the_gateway_is_not_part_of_make_up() -> None:
    assert SERVICE["profiles"] == ["gateway"]


def test_secrets_come_from_the_environment_with_dummy_defaults() -> None:
    defaults = {}
    for name in ("INITIAL_PASSWORD", "JWT_SECRET", "API_KEY_SECRET", "STORAGE_ENCRYPTION_KEY"):
        match = re.fullmatch(r"\$\{OMNIROUTE_[A-Z_]+:-(surakshasetu-dev-[a-z-]+)\}", ENV[name])
        assert match, name
        defaults[name] = match[1]
    assert "CHANGEME" not in json.dumps(ENV)
    # OmniRoute 3.8.50 refuses to start below these lengths.
    assert len(defaults["JWT_SECRET"]) >= 32
    assert len(defaults["API_KEY_SECRET"]) >= 16


def test_every_request_needs_an_api_key() -> None:
    assert ENV["REQUIRE_API_KEY"] == "true"


def test_built_in_guardrails_are_on_as_a_second_line() -> None:
    assert (ENV["INPUT_SANITIZER_ENABLED"], ENV["INJECTION_GUARD_MODE"]) == ("true", "warn")
    assert ENV["PII_REDACTION_ENABLED"] == "true"


def test_fingerprinting_and_proxies_are_off() -> None:
    assert (ENV["ENABLE_TLS_FINGERPRINT"], ENV["ENABLE_SOCKS5_PROXY"]) == ("false", "false")
    settings = SEED["settings"]
    assert (settings["proxyEnabled"], settings["perKeyProxyEnabled"]) == (False, False)
    assert settings["cliCompatProviders"] == []


def test_mcp_and_a2a_are_off_and_no_server_key_exists() -> None:
    assert (SEED["settings"]["mcpEnabled"], SEED["settings"]["a2aEnabled"]) == (False, False)
    assert ENV["OMNIROUTE_MCP_ENFORCE_SCOPES"] == "true"
    # A2A routes with the server's own key, which would bypass the app key's policy.
    assert "OMNIROUTE_API_KEY" not in ENV
    assert not {"manage", "admin", "mcp:connect"} & set(KEY["scopes"])


def test_feature_flags_the_verifier_expects_are_the_compose_values() -> None:
    assert {name: ENV[name] for name in SEED["featureFlags"]} == SEED["featureFlags"]
    assert ENV["OMNIROUTE_EMERGENCY_FALLBACK"] == "false"


def test_the_gateway_adds_no_queue_or_hidden_waits() -> None:
    # 3.8.50 queues API-key connections by default: 60 requests a minute, 350 ms apart, waits
    # of up to 15 s; and it waits out cooldowns for up to 30 s. Every route budget is below that.
    assert SEED["resilience"] == {
        "requestQueue": {"autoEnableApiKeyProviders": False},
        "waitForCooldown": {"enabled": False},
        "comboCooldownWait": {"enabled": False},
    }


def test_combos_are_exactly_the_chat_routes_on_the_priority_strategy() -> None:
    combos = SEED["import"]["combos"]
    targets = [target for combo in combos for target in combo["models"]]

    assert {combo["name"] for combo in combos} == CHAT_ROUTES
    assert {combo["strategy"] for combo in combos} == {"priority"}
    assert all(combo["config"] == {"maxRetries": 0} for combo in combos)
    # One stub per combo, so the served model names the route that asked.
    assert len(set(targets)) == len(combos) == len(targets)
    assert all(target.startswith("vllm/") for target in targets)


def test_auto_and_adaptive_routing_are_off() -> None:
    assert SEED["settings"]["autoRoutingEnabled"] is False
    assert SEED["settings"]["adaptiveVolumeRouting"] is False


def test_the_only_upstream_is_the_self_hosted_stub_connection() -> None:
    # vllm, not an openai-compatible-* node: those rewrite json_schema into a system message.
    (connection,) = SEED["import"]["providerConnections"]
    assert connection["provider"] == "vllm"
    assert connection["providerSpecificData"] == {"baseUrl": "http://stubs:8090/v1"}
    assert "providerNodes" not in SEED["import"]
    assert KEY["allowedConnections"] == [connection["id"]]


def test_compression_memory_and_semantic_cache_are_off() -> None:
    compression = SEED["compression"]
    assert (compression["enabled"], compression["defaultMode"]) == (False, "off")
    assert compression["autoTriggerTokens"] == 0
    assert SEED["memory"] == {"enabled": False, "skillsEnabled": False}
    # Chat reads the flat setting, which only the import writes; the database copy is display.
    assert SEED["import"]["settings"]["semanticCacheEnabled"] is False
    assert SEED["database"]["cache"]["semanticCacheEnabled"] is False
    assert KEY["compressionEnabled"] is False
    assert KEY["cacheDefaultMode"] == "bypass"


def test_nothing_copies_prompts_into_gateway_logs() -> None:
    assert KEY["noLog"] is True
    assert SEED["database"]["logs"] == {
        "detailedLogsEnabled": False,
        "callLogPipelineEnabled": False,
    }


def test_the_app_key_reaches_only_the_chat_combos() -> None:
    assert KEY["modelAccessMode"] == "restricted"
    assert KEY["allowedModels"] == []
    assert set(KEY["allowedCombos"]) == CHAT_ROUTES
    assert KEY["allowedEndpoints"] == ["chat"]
    # True would make OmniRoute skip our own vllm/stub-* combo targets as "non-public"; auto/*
    # (what it guards) is closed by autoRoutingEnabled=false instead.
    assert KEY["disableNonPublicModels"] is False
    # The key's value is SS_GATEWAY_API_KEY, never Git.
    assert all("key" not in key for key in SEED["import"]["apiKeys"])


def test_keyless_providers_are_blocked() -> None:
    blocked = set(SEED["settings"]["blockedProviders"])
    assert {"opencode", "duckduckgo-web", "felo-web", "aihorde"} <= blocked
    assert SEED["settings"]["noAuthFallbackDisabledProviders"]


def test_every_hardening_row_names_tests_that_exist() -> None:
    rows = [
        line
        for line in (INFRA / "omniroute" / "HARDENING.md").read_text().splitlines()
        if line.startswith("| ") and "`test_" in line
    ]
    named = [ref for row in rows for ref in re.findall(r"`(test_\w+\.py)::(test_\w+)`", row)]

    assert len(rows) >= 11  # one per TDD §1.6 row, plus this step's additions
    for file, function in named:
        (source,) = ORCHESTRATOR_TESTS.glob(f"*/{file}")
        assert f"def {function}(" in source.read_text(), f"{file}::{function}"
