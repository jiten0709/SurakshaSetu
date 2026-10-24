"""The chat UI's compose service, profile app (since 2026-10-23): Vite's dev server in a container,
so `make app-up` needs no `make ui` terminal. Read from the files: starting it is `make app-up`.
The source is bind-mounted, node_modules lives in a volume, the proxy reaches the `orchestrator`
service, and no credential is set in compose (the inspector's key comes with frontend/.env.local).
"""

import re
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[3]
COMPOSE: dict[str, Any] = yaml.safe_load((REPO / "infra" / "compose.yaml").read_text("utf-8"))
UI = COMPOSE["services"]["ui"]
DOCKERFILE = (REPO / "frontend" / "Dockerfile").read_text("utf-8")
MAKEFILE = (REPO / "Makefile").read_text("utf-8")
VITE_CONFIG = (REPO / "frontend" / "vite.config.ts").read_text("utf-8")


def test_the_ui_is_published_on_the_host_only_and_waits_for_the_api() -> None:
    api = COMPOSE["services"]["orchestrator"]

    assert UI["profiles"] == ["app"]
    assert UI["ports"] == ["127.0.0.1:5173:5173"]
    assert UI["depends_on"]["orchestrator"] == {"condition": "service_healthy"}
    # The proxy target is the API's service name and its container port.
    assert UI["environment"] == {"UI_API_TARGET": "http://orchestrator:8000"}
    assert api["ports"] == ["127.0.0.1:8000:8000"]


def test_the_dev_server_listens_in_the_container_and_installs_only_when_the_lock_changed() -> None:
    script = UI["command"][-1]

    assert UI["command"][:2] == ["sh", "-c"]
    assert "[ node_modules/.package-lock.json -nt package-lock.json ] || npm ci" in script
    assert "npm run dev -- --host 0.0.0.0" in script
    assert "[ node_modules/.package-lock.json -nt package-lock.json ] || npm ci" in MAKEFILE


def test_node_modules_is_isolated_from_the_hosts_in_a_named_volume() -> None:
    assert "../frontend:/app" in UI["volumes"]
    assert "ui-node-modules:/app/node_modules" in UI["volumes"]
    assert "ui-node-modules" in COMPOSE["volumes"]


def test_no_credential_is_set_in_compose_or_the_image() -> None:
    names = " ".join(UI["environment"]).upper()
    users = re.findall(r"^USER (\S+)", DOCKERFILE, re.MULTILINE)
    rules = (REPO / "frontend" / ".dockerignore").read_text("utf-8").splitlines()
    rules = [rule for rule in rules if rule and not rule.startswith("#")]

    assert not re.search(r"KEY|TOKEN|PASSWORD|SECRET", names)
    assert users and users[-1] not in ("root", "0")
    assert re.search(r"^FROM node:24\.\d+\.\d+-slim$", DOCKERFILE, re.MULTILINE)
    assert "COPY" not in DOCKERFILE and rules == ["*"]


def test_app_up_starts_it_and_both_proxy_entries_follow_the_target() -> None:
    app_up = re.search(r"^app-up:\n\t(.*)$", MAKEFILE, re.MULTILINE)

    assert app_up and app_up.group(1).endswith("orchestrator scheduler ui")
    # One literal remains, as the host-side fallback; both proxy entries use the variable.
    assert VITE_CONFIG.count("http://127.0.0.1:8000") == 1
    assert 'process.env.UI_API_TARGET ?? "http://127.0.0.1:8000"' in VITE_CONFIG
