"""TDD §1.6 as written in Git: the omniroute compose service and infra/omniroute/seed.json.

No network. tests/stack/test_omniroute_stack.py proves the running gateway matches and behaves;
infra/omniroute/HARDENING.md maps each row to the tests here and there.
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from surakshasetu.config import Settings
from surakshasetu.eval import gateway_log
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


def test_the_live_image_is_the_one_bug_43s_gate_was_verified_on() -> None:
    # The gate reads 3.8.50's REASONING_SANITIZE tag and log format (decision log §2). A renamed or
    # silent tag on another image would pass it, so an image change stops here first.
    assert LIVE_SERVICE["image"] == gateway_log.VERIFIED_IMAGE, (
        "omniroute-live's image changed: re-run a clamp probe on it, recheck the tag, the HTTP"
        " event and the log format in eval/gateway_log.py, then update VERIFIED_IMAGE"
    )


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


LIVE_TARGETS = (
    "gateway-live-up",
    "gateway-live-verify",
    "gateway-live-down",
    "check-gateway-live",
    "eval-live-self-hosted",  # C6
)
LIVE_VARIABLES = (
    "LIVE_URL",
    "LIVE_ENV",
    "LIVE_SEED",
    "LIVE_COMPOSE",
    "LIVE_UP",
    "LIVE_DOWN",
    "LIVE_SCALE",
    "LIVE_CONTEXT",
    "LIVE_EVAL_ENV",
    "LIVE_GATE",  # bug #43
)
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
    # The log goes where LIVE_LOG says (eval-live-self-hosted's exact path), else the old name.
    assert (
        'log="$${LIVE_LOG:-orchestrator/.cache/live-gateway-$$(date -u +%Y%m%dT%H%M%SZ).log}";'
        in down
    )
    # Starting targets: signals only recorded; cleanup with signals ignored; status preserved.
    up, check = recipe("gateway-live-up"), recipe("check-gateway-live")
    evaluation = recipe("eval-live-self-hosted")
    for body in (up, check, evaluation):
        assert SIGNALS in body and "trap '' INT TERM;" in body and "exit $${sig:-$$rc}" in body
    assert 'if [ -n "$$sig" ] || [ "$$rc" -ne 0 ]; then' in up
    for body in (check, evaluation):  # the run only after a clean start; cleanup whatever happened
        assert 'if [ "$$rc" -eq 0 ] && [ -z "$$sig" ]; then' in body
        assert '[ "$$rc" -ne 0 ] || rc=1; };' in body and "$(LIVE_DOWN) ||" in body
    assert check.count("pytest") == 1 and "tests/stack/test_gateway_live_stack.py)" in check
    assert "--profile gateway-live" in recipe("down")


def test_the_self_hosted_evaluation_runs_against_the_live_gateway_only() -> None:
    # C6 (D-4): seeded by gateway-live-up (it refuses __UNSET__ until C5b), then the report on
    # :20131 with the context check (D-6) and the chat scale (at most 15, D-2).
    evaluation = recipe("eval-live-self-hosted")
    assert '$(MAKE) --no-print-directory -C "$(CURDIR)" gateway-live-up || rc=$$?;' in evaluation
    assert (
        '$(call EVAL_REPORT,$(LIVE_EVAL_ENV)) --mode live --routes self-hosted --stamp "$$stamp")'
        in evaluation
    )
    assert variable("LIVE_EVAL_ENV").split() == [
        "SS_TEI_TIMEOUT_SCALE=1000",
        "SS_GATEWAY_BASE_URL=$(LIVE_URL)",
        "\\",
        "SS_GATEWAY_TIMEOUT_SCALE=$(LIVE_SCALE)",
        "SS_GATEWAY_CONTEXT_TOKENS=$(LIVE_CONTEXT)",
    ]
    assert (variable("LIVE_SCALE").strip(), variable("LIVE_CONTEXT").strip()) == ("15", "8192")
    # Bug #43's gate: the eval CLI's gateway-log step, nothing else (the wiring is run below).
    assert variable("LIVE_GATE").strip() == (
        "cd orchestrator && SS_LOG_FORMAT=text uv run --locked python -m surakshasetu.eval"
        " gateway-log"
    )
    # EVAL_REPORT's argument follows GOLDEN_ENV, so the live URL overrides its :20130 (in sh the
    # last assignment wins), and make eval-live is unchanged.
    assert re.search(
        r"^EVAL_REPORT = cd orchestrator && \$\(GOLDEN_ENV\) .*\$\(1\)", MAKEFILE, re.M
    )
    assert recipe("eval-live") == (
        "\t@rm -f reports/eval-latest.md; rc=0; ($(call EVAL_REPORT,SS_TEI_TIMEOUT_SCALE=1000)"
        " --mode live) \\\n\t\t|| rc=$$?; \\\n"
        "\t\tcat reports/eval-latest.md 2>/dev/null; exit $$rc\n"
    )


DEFS = dict(re.findall(r"^(\w+) :=((?:.*\\\n)*.*)$", MAKEFILE, re.MULTILINE))
INERT = {"COMPOSE": "docker compose", "MAKE": "make", "CURDIR": "/repo"}
INERT["OMNIROUTE_INITIAL_PASSWORD"] = "placeholder"  # noqa: S105 (inert, never a secret)


def script(body: str, **inert: str) -> str:
    """A recipe as one sh script. LIVE_* variables expand from the Makefile unless given; every
    other make variable must be given, so no infra/.env value is ever expanded."""

    def expand(text: str) -> str:
        def one(match: re.Match[str]) -> str:
            name = match[1]
            if name in inert:
                return inert[name]
            assert name.startswith("LIVE_"), name
            return expand(DEFS[name].replace("\\\n", " "))

        return re.sub(r"\$\((\w+)\)", one, text)

    assert all(line.startswith("\t") for line in body.splitlines())  # recipe lines: tabs
    lines = "\n".join(line[1:] for line in body.splitlines())
    return expand(lines).lstrip("@").replace("$$", "$")


def test_the_live_recipes_parse_under_sh() -> None:
    """Each recipe through `/bin/sh -n` (parse only: nothing runs), with inert placeholders."""
    for target in LIVE_TARGETS:
        parsed = subprocess.run(  # noqa: S603
            ["/bin/sh", "-n", "-c", script(recipe(target), **INERT)], capture_output=True
        )
        assert parsed.returncode == 0, f"{target}: {parsed.stderr.decode()}"


# Stand-ins for the run's three steps, so the recipe runs under sh with no Docker, uv or network:
# make (gateway-live-up exits $UP_RC; gateway-live-down "saves" $FIXTURE at $LIVE_LOG), the report
# step and the gate, each recording its arguments.
FAKES = {
    "fake_make": 'case "$*" in *-up) exit "$UP_RC";; *-down) mkdir -p "${LIVE_LOG%/*}"'
    ' && cp "$FIXTURE" "$LIVE_LOG" && echo "$LIVE_LOG" > down.args;; esac',
    "fake_report": 'echo "$@" > report.args; exit "$REPORT_RC"',
    "fake_gate": 'echo "$@" > gate.args; exit "$GATE_RC"',
}


@pytest.mark.parametrize(
    ("up", "evaluated", "gated", "status"),
    [(0, 0, 0, 0), (0, 0, 1, 1), (0, 1, 0, 1), (0, 2, 2, 2), (1, 0, 0, 1)],
)
def test_the_self_hosted_evaluation_gates_the_log_its_own_teardown_saved(
    tmp_path: Path, up: int, evaluated: int, gated: int, status: int
) -> None:
    """Bug #43: after teardown the gate reads exactly the log this run's gateway-live-down saved,
    with the report's stamp; its failure fails the target even when the report step passed; it
    doesn't run when gateway-live-up failed (no report step ran)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    fixture = tmp_path / "container.log"
    fixture.write_text("omniroute-live-1  | the container's log\n")
    body = recipe("eval-live-self-hosted").replace(
        "$(call EVAL_REPORT,$(LIVE_EVAL_ENV))", "fake_report"
    )
    sh = script(body, MAKE="fake_make", CURDIR=str(tmp_path), LIVE_GATE="fake_gate")
    assert "fake_report --mode live --routes self-hosted" in sh  # the stand-in took its place
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FIXTURE": str(fixture), "UP_RC": str(up),
           "REPORT_RC": str(evaluated), "GATE_RC": str(gated)}  # fmt: skip
    ran = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", sh], cwd=tmp_path, env=env, capture_output=True
    )
    assert ran.returncode == status, ran.stderr.decode()
    if up:
        assert not (tmp_path / "report.args").exists() and not (tmp_path / "gate.args").exists()
        return
    saved = (tmp_path / "down.args").read_text().strip()
    reported = (tmp_path / "report.args").read_text().split()
    stamp = reported[reported.index("--stamp") + 1]
    assert saved == f"orchestrator/.cache/live-gateway-{stamp}.log"
    assert (tmp_path / "gate.args").read_text().split() == [
        "--log", f"{tmp_path}/{saved}", "--stamp", stamp,
    ]  # fmt: skip
    assert (tmp_path / saved).read_bytes() == fixture.read_bytes()  # read in place, unchanged


# A stand-in for `docker compose`: ps lists one id until rm, logs writes one line to stdout and one
# to stderr, rm marks the container removed; anything else exits 2. It records every call, and for
# logs the files its stdout and stderr are (device and inode), since the recipe names the log only
# through its redirection.
FAKE_COMPOSE = r"""
import os
import sys
from pathlib import Path

state = Path(os.environ["FAKE_STATE"])
args = sys.argv[1:]
with (state / "calls").open("a") as calls:
    calls.write(" ".join(args) + "\n")
command = args[2] if args[:2] == ["--profile", "gateway-live"] and len(args) > 2 else ""
if command == "ps":
    print("" if (state / "removed").exists() else "0123456789ab")
elif command == "logs":
    fds = [os.fstat(fd) for fd in (1, 2)]
    (state / "logs.fds").write_text(" ".join(f"{s.st_dev}:{s.st_ino}" for s in fds))
    print("omniroute-live-1  | 2026-11-17T00:00:00.000000000Z the container's line", flush=True)
    print("compose's own stderr line", file=sys.stderr, flush=True)
elif command == "rm":
    (state / "removed").touch()
else:
    sys.exit(2)
"""


@pytest.mark.parametrize("live_log", [True, False])
def test_gateway_live_down_saves_the_log_exactly_where_live_log_says(
    tmp_path: Path, live_log: bool
) -> None:
    """Bug #43: the real gateway-live-down recipe under /bin/sh with a fake `docker compose` on a
    restricted PATH (no Docker, nothing real touched). With LIVE_LOG set, the log is exactly that
    file (the fake's stdout and stderr, 2>&1) and nothing is written under the default name;
    unset, the old timestamped name in orchestrator/.cache."""
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    fake = bin_dir / "fake_compose"
    fake.write_text(f"#!{sys.executable} -I\n{FAKE_COMPOSE}")
    fake.chmod(0o755)
    sh = script(recipe("gateway-live-down"), COMPOSE="fake_compose")
    assert "docker" not in sh and "fake_compose --profile gateway-live" in sh
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_STATE": str(state)}
    chosen = tmp_path / "chosen.log"
    if live_log:
        env["LIVE_LOG"] = str(chosen)
    ran = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", sh], cwd=tmp_path, env=env, capture_output=True
    )
    assert ran.returncode == 0, ran.stderr.decode()
    assert (state / "calls").read_text().splitlines() == [
        "--profile gateway-live ps --all --quiet omniroute-live",
        "--profile gateway-live logs --no-color --timestamps omniroute-live",
        "--profile gateway-live rm --stop --force omniroute-live",
        "--profile gateway-live ps --all --quiet omniroute-live",
    ]
    cache = sorted((tmp_path / "orchestrator" / ".cache").iterdir())
    if live_log:
        saved = chosen
        assert cache == []  # nothing under the default name
    else:
        assert not chosen.exists()
        (saved,) = cache
        assert re.fullmatch(r"live-gateway-\d{8}T\d{6}Z\.log", saved.name)
    found = saved.stat()
    assert (state / "logs.fds").read_text() == " ".join([f"{found.st_dev}:{found.st_ino}"] * 2)
    assert saved.read_text().splitlines() == [
        "omniroute-live-1  | 2026-11-17T00:00:00.000000000Z the container's line",
        "compose's own stderr line",
    ]


def test_the_stub_targets_are_unchanged_and_pinned_to_the_stub_gateway() -> None:
    assert recipe("gateway-up") == (
        "\t$(COMPOSE) --profile gateway up -d --build --wait omniroute\n\t@$(SEED) apply\n"
    )
    assert recipe("gateway-verify") == "\t@$(SEED) verify\n"
    assert "SS_GATEWAY_BASE_URL=http://127.0.0.1:20130/v1" in variable("GOLDEN_ENV")
    assert "20131" not in recipe("start") + variable("SEED") + variable("GOLDEN_ENV")
    assert "--ignore=tests/stack/test_gateway_live_stack.py" in recipe("check-stack")
