"""TDD §1.6 as written in Git: the omniroute compose service and infra/omniroute/seed.json.

No network. tests/stack/test_omniroute_stack.py proves the running gateway matches and behaves;
infra/omniroute/HARDENING.md maps each row to the tests here and there.
"""

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

from surakshasetu.config import Settings
from surakshasetu.gateway import Route
from surakshasetu.gateway.adapter import ROUTES

INFRA = Path(__file__).parents[3] / "infra"
ORCHESTRATOR_TESTS = Path(__file__).parents[1]
SERVICES: dict[str, Any] = yaml.safe_load((INFRA / "compose.yaml").read_text())["services"]
SERVICE: dict[str, Any] = SERVICES["omniroute"]
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


# --- the live instance (Step 1.5b, design E, D-1) -------------------------------------------------
LIVE_SERVICE: dict[str, Any] = SERVICES["omniroute-live"]
LIVE_SEED: dict[str, Any] = json.loads((INFRA / "omniroute" / "seed.live.json").read_text())
MAKEFILE = (INFRA.parent / "Makefile").read_text("utf-8")
STUB_ID, LIVE_ID = "0199a900-0000-7000-8000-000000000001", "0199a900-0000-7000-8000-000000000002"
# `omniroute` exactly as it was before omniroute-live existed. Any change to it recreates the stub
# container (whose reboot may rewrite /app/data/server.env), so it changes only on purpose.
FROZEN_OMNIROUTE = {
    "image": "diegosouzapw/omniroute:3.8.50"
    "@sha256:085c57adf499a8aaa9f35ccde95c0df9c11bd9ecd18d6c9edbf3b68b8079ba9d",
    "profiles": ["gateway"],
    "depends_on": {"stubs": {"condition": "service_healthy"}},
    "environment": {
        "INITIAL_PASSWORD": "${OMNIROUTE_INITIAL_PASSWORD:-surakshasetu-dev-omniroute}",
        "JWT_SECRET": "${OMNIROUTE_JWT_SECRET:-surakshasetu-dev-omniroute-jwt-signing}",
        "API_KEY_SECRET": "${OMNIROUTE_API_KEY_SECRET:-surakshasetu-dev-omniroute-api-key}",
        "STORAGE_ENCRYPTION_KEY": (
            "${OMNIROUTE_STORAGE_ENCRYPTION_KEY:-surakshasetu-dev-omniroute-storage}"
        ),
        "REQUIRE_API_KEY": "true",
        "INPUT_SANITIZER_ENABLED": "true",
        "INJECTION_GUARD_MODE": "warn",
        "PII_REDACTION_ENABLED": "true",
        "ENABLE_TLS_FINGERPRINT": "false",
        "ENABLE_SOCKS5_PROXY": "false",
        "OMNIROUTE_MCP_ENFORCE_SCOPES": "true",
        "OMNIROUTE_EMERGENCY_FALLBACK": "false",
    },
    "ports": ["127.0.0.1:20130:20128"],
    "volumes": ["omniroute-data:/app/data"],
    "stop_grace_period": "40s",
    "healthcheck": {
        "interval": "5s",
        "timeout": "5s",
        "retries": 30,
        "test": ["CMD", "node", "healthcheck.mjs"],
        "start_period": "60s",
    },
}


def recipe(target: str) -> str:
    found = re.search(rf"^{re.escape(target)}:.*\n((?:\t.*\n)+)", MAKEFILE, re.MULTILINE)
    assert found, f"no rule for {target}"
    return found.group(1)


def variable(name: str) -> str:
    found = re.search(rf"^{name} :=((?:.*\\\n)*.*)$", MAKEFILE, re.MULTILINE)
    assert found, f"no variable {name}"
    return found.group(1)


def test_the_stub_instance_is_unchanged() -> None:
    assert SERVICE == FROZEN_OMNIROUTE
    assert "omniroute-data" in yaml.safe_load((INFRA / "compose.yaml").read_text())["volumes"]


def test_the_live_instance_is_the_same_gateway_with_no_state_of_its_own() -> None:
    assert LIVE_SERVICE["image"] == SERVICE["image"]
    assert LIVE_SERVICE["profiles"] == ["gateway-live"]
    assert LIVE_SERVICE["depends_on"] == SERVICE["depends_on"]
    assert LIVE_SERVICE["healthcheck"] == SERVICE["healthcheck"]
    # The stub's hardening env, duplicated, plus only the response-start bound (O2, D-2).
    assert LIVE_SERVICE["environment"] == SERVICE["environment"] | {
        "OMNIROUTE_DIRECT_HEADERS_TIMEOUT_MS": "105000"
    }
    assert "volumes" not in LIVE_SERVICE  # its database dies with the container
    assert LIVE_SERVICE["ports"] == ["127.0.0.1:20131:20128"]
    assert LIVE_SERVICE["restart"] == "no"


def test_the_bound_sits_between_the_scaled_budgets_and_the_combo_timeout() -> None:
    bound_s = int(LIVE_SERVICE["environment"]["OMNIROUTE_DIRECT_HEADERS_TIMEOUT_MS"]) / 1000
    largest = max(spec.timeout_s for route, spec in ROUTES.items() if route.value in CHAT_ROUTES)
    cap = Settings.model_fields["gateway_timeout_scale"].metadata
    scale = next(m.le for m in cap if hasattr(m, "le"))
    # The adapter gives up before the gateway re-sends; 120 s is OmniRoute 3.8.50's combo timeout.
    assert largest * scale < bound_s < 120


def test_the_live_overlay_adds_one_connection_and_moves_only_the_self_hosted_routes() -> None:
    (connection,) = LIVE_SEED["providerConnections"]
    assert connection["id"] == LIVE_ID
    assert connection["provider"] == "ollama-local"  # a provider type distinct from vllm
    assert connection["providerSpecificData"] == {"baseUrl": "http://host.docker.internal:11434/v1"}
    combos = LIVE_SEED["combos"]
    assert set(combos) == CHAT_ROUTES - {"gen-converse", "gen-recommend"}  # O4: gen stays stubbed
    assert all(len(targets) == 1 for targets in combos.values())  # one target, no fallover
    assert {t.split("/")[0] for targets in combos.values() for t in targets} == {
        "ollama",
        "ollama-local",
    }  # both target forms, as U7 tested
    assert LIVE_SEED["allowedConnections"] == [STUB_ID, LIVE_ID]
    # No other section: everything else (hardening included) is seed.json's, and there is no
    # global fallback model anywhere.
    assert set(LIVE_SEED) == {"$comment", "providerConnections", "combos", "allowedConnections"}
    assert "globalFallbackModel" not in json.dumps(SEED) + json.dumps(LIVE_SEED)


LIVE_TARGETS = ("gateway-live-up", "gateway-live-verify", "gateway-live-down", "check-gateway-live")
LIVE_VARIABLES = ("LIVE_URL", "LIVE_ENV", "LIVE_SEED", "LIVE_COMPOSE", "LIVE_UP", "LIVE_DOWN")
SIGNALS = "sig=; rc=0; trap 'sig=130' INT; trap 'sig=143' TERM;"


def test_the_live_targets_touch_only_the_live_instance() -> None:
    recipes = "".join(recipe(t) for t in LIVE_TARGETS)
    live = recipes + "\n".join(variable(v) for v in LIVE_VARIABLES)
    assert "20131" in live and "20130" not in live
    # No bare docker call, volume, compose down or stub profile.
    assert "docker" not in live and "$(COMPOSE)" not in recipes
    forbidden = r"omniroute-data|\bvolumes?\b|\s-v\b|\sdown\b|--profile gateway(?![\w-])"
    assert not re.search(forbidden, live)
    # Every compose call goes through LIVE_COMPOSE and names the omniroute-live service.
    calls = {c.strip(" \\") for c in re.findall(r"\$\(LIVE_COMPOSE\) ([^;)&>\n]*)", live)}
    assert calls == {
        "up -d --no-deps --force-recreate --wait omniroute-live",
        "ps --all --quiet omniroute-live",
        "logs --no-color --timestamps omniroute-live",
        "rm --stop --force omniroute-live",
    }
    # Both listings in gateway-live-down are status-checked; removal is confirmed by the second.
    down = recipe("gateway-live-down")
    assert down.count("ps --all --quiet omniroute-live) \\\n\t\t|| { echo") == 2
    assert '[ -z "$$left" ] ||' in down
    assert "saving the log failed; removing omniroute-live anyway" in down
    # Starting targets: signals only recorded; cleanup with signals ignored; status preserved.
    up, check = recipe("gateway-live-up"), recipe("check-gateway-live")
    for body in (up, check):
        assert SIGNALS in body and "trap '' INT TERM;" in body and "exit $${sig:-$$rc}" in body
    assert 'if [ -n "$$sig" ] || [ "$$rc" -ne 0 ]; then' in up
    assert '[ "$$rc" -ne 0 ] || rc=1; };' in check
    assert check.count("pytest") == 1 and "tests/stack/test_gateway_live_stack.py)" in check
    assert "--profile gateway-live" in recipe("down")


def test_the_live_recipes_parse_under_sh() -> None:
    """Each recipe through `/bin/sh -n` (parse only: nothing runs). make's variables are replaced
    by inert placeholders, so no infra/.env value is expanded."""
    defs = dict(re.findall(r"^(\w+) :=((?:.*\\\n)*.*)$", MAKEFILE, re.MULTILINE))
    inert = {"COMPOSE": "docker compose", "MAKE": "make", "CURDIR": "/repo"}
    inert["OMNIROUTE_INITIAL_PASSWORD"] = "placeholder"  # noqa: S105 (inert, never a secret)

    def expand(text: str) -> str:
        def one(match: re.Match[str]) -> str:
            name = match[1]
            if name.startswith("LIVE_"):
                return expand(defs[name].replace("\\\n", " "))
            return inert[name]

        return re.sub(r"\$\((\w+)\)", one, text)

    for target in LIVE_TARGETS:
        body = recipe(target)
        assert all(line.startswith("\t") for line in body.splitlines())  # recipe lines: tabs
        script = expand("\n".join(line[1:] for line in body.splitlines()))
        script = script.lstrip("@").replace("$$", "$")
        parsed = subprocess.run(["/bin/sh", "-n", "-c", script], capture_output=True)  # noqa: S603
        assert parsed.returncode == 0, f"{target}: {parsed.stderr.decode()}"


def test_the_stub_targets_are_unchanged_and_pinned_to_the_stub_gateway() -> None:
    assert recipe("gateway-up") == (
        "\t$(COMPOSE) --profile gateway up -d --build --wait omniroute\n\t@$(SEED) apply\n"
    )
    assert recipe("gateway-verify") == "\t@$(SEED) verify\n"
    assert "SS_GATEWAY_BASE_URL=http://127.0.0.1:20130/v1" in variable("GOLDEN_ENV")
    assert "20131" not in recipe("start") + variable("SEED") + variable("GOLDEN_ENV")
    assert "--ignore=tests/stack/test_gateway_live_stack.py" in recipe("check-stack")
