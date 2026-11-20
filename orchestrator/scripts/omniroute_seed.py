"""Apply and verify infra/omniroute/seed.json on SurakshaSetu's OmniRoute (Step 9).

    uv run python scripts/omniroute_seed.py apply           # make gateway-up
    uv run python scripts/omniroute_seed.py verify          # make gateway-verify
    uv run python scripts/omniroute_seed.py apply --live    # make gateway-live-up (Step 1.5b)
    uv run python scripts/omniroute_seed.py verify --live   # make gateway-live-verify

OmniRoute keeps its configuration in SQLite behind an admin API, so Git holds the seed and this
script puts it there. apply logs in, imports the connection, combos and app key (OmniRoute's own
import format, upserted by id, so a re-run is idempotent), sets each settings section and the app
key's policy, then verifies. verify reads every seeded value back, since OmniRoute answers 200 to
keys it drops, and logs each drift; the exit status is 1 on any.

--live merges infra/omniroute/seed.live.json over the seed in memory (neither file is written) for
the ephemeral omniroute-live instance (design E). Each seed goes only to its own instance: --live
needs the live port (20131), and without it the live port is refused, so a misdirected run can't
write the other instance's database. The overlay's payload rules (C5b: thinking off) go through
their own endpoint and are read back exactly; the stub seed has none, so it sends no such request.
apply --live refuses a model left as __UNSET__.

Reads SS_GATEWAY_BASE_URL (the gateway), SS_GATEWAY_API_KEY (the app key's value) and
OMNIROUTE_INITIAL_PASSWORD (the admin password; compose's dummy by default).
"""

import copy
import json
import logging
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from pydantic import SecretStr

from surakshasetu.config import load_settings
from surakshasetu.logging import configure_logging

logger = logging.getLogger("surakshasetu.gateway.omniroute_seed")

SEED_PATH = Path(__file__).resolve().parents[2] / "infra" / "omniroute" / "seed.json"
LIVE_PATH = SEED_PATH.with_name("seed.live.json")
LIVE_PORT = 20131  # omniroute-live; the stub instance is 20130
UNSET = "__UNSET__"  # seed.live.json's model before C5b named one; apply --live refuses it
# compose's INITIAL_PASSWORD default
DEV_ADMIN_PASSWORD = SecretStr("surakshasetu-dev-omniroute")


class SeedError(RuntimeError):
    """An admin call failed. The message carries the method, path and status only."""


def admin_password() -> str:
    return os.environ.get("OMNIROUTE_INITIAL_PASSWORD") or DEV_ADMIN_PASSWORD.get_secret_value()


def load_seed(live: bool = False) -> dict[str, Any]:
    """seed.json; with live, seed.live.json merged over it: its connections added, its combos'
    targets replacing theirs, its allowedConnections replacing the key's, its payload rules."""
    seed: dict[str, Any] = json.loads(SEED_PATH.read_text())
    if live:
        overlay = json.loads(LIVE_PATH.read_text())
        seed["import"]["providerConnections"] += overlay["providerConnections"]
        for combo in seed["import"]["combos"]:
            combo["models"] = overlay["combos"].get(combo["name"], combo["models"])
        seed["appKeyPolicy"]["allowedConnections"] = overlay["allowedConnections"]
        seed["payloadRules"] = overlay["payloadRules"]
    return seed


def check_target(gateway_base_url: str, live: bool) -> str | None:
    """Why this seed must not go to this gateway, or None."""
    port = httpx.URL(gateway_base_url).port
    if live and port != LIVE_PORT:
        return f"--live writes only to omniroute-live on :{LIVE_PORT}, not :{port}"
    if not live and port == LIVE_PORT:
        return f"the stub seed never goes to omniroute-live (:{LIVE_PORT}); add --live"
    return None


@contextmanager
def admin_client(gateway_base_url: str, password: str) -> Iterator[httpx.Client]:
    """A client logged in to the admin API (the gateway's /v1 base without /v1)."""
    base = gateway_base_url.rstrip("/").removesuffix("/v1")
    with httpx.Client(base_url=base, timeout=30.0) as client:
        _ok(client.post("/api/auth/login", json={"password": password}))
        yield client


def apply(client: httpx.Client, seed: dict[str, Any], app_key: str) -> None:
    imported = copy.deepcopy(seed["import"])
    for key in imported["apiKeys"]:
        key["key"] = app_key
    _ok(client.post("/api/settings/import-json", json=imported))
    _ok(client.patch("/api/settings", json=seed["settings"]))
    # Each database section must be sent whole: keep the fields the seed doesn't set.
    current = _ok(client.get("/api/settings/database"))
    database = {name: current[name] | values for name, values in seed["database"].items()}
    _ok(client.patch("/api/settings/database", json=database))
    _ok(client.patch("/api/resilience", json=seed["resilience"]))
    _ok(client.put("/api/settings/memory", json=seed["memory"]))
    _ok(client.put("/api/settings/compression", json=seed["compression"]))
    if "payloadRules" in seed:  # the live seed only; PATCH /api/settings drops them with a 200
        _ok(client.put("/api/settings/payload-rules", json=seed["payloadRules"]))
    # The import resets the key's restrictions, so the policy always goes on afterwards.
    _ok(client.patch(f"/api/keys/{_app_key_id(seed)}", json=seed["appKeyPolicy"]))
    logger.info("omniroute seed applied")


def verify(client: httpx.Client, seed: dict[str, Any]) -> list[str]:
    """Every seeded value as the gateway reports it; one line per difference."""
    drift: list[str] = []
    settings = _ok(client.get("/api/settings"))
    _compare("settings", seed["settings"] | seed["import"]["settings"], settings, drift)
    if settings.get("requireLogin") is not True:
        drift.append("settings.requireLogin: expected True")
    _compare("database", seed["database"], _ok(client.get("/api/settings/database")), drift)
    _compare("resilience", seed["resilience"], _ok(client.get("/api/resilience")), drift)
    _compare("memory", seed["memory"], _ok(client.get("/api/settings/memory")), drift)
    _compare(
        "compression", seed["compression"], _ok(client.get("/api/settings/compression")), drift
    )
    if "payloadRules" in seed:  # the GET lists every section; the empty ones aren't seeded
        rules = {k: v for k, v in _ok(client.get("/api/settings/payload-rules")).items() if v}
        if rules != seed["payloadRules"]:
            drift.append(f"payloadRules: expected {seed['payloadRules']!r}, got {rules!r}")
    flags = {f["key"]: f for f in _ok(client.get("/api/settings/feature-flags"))["flags"]}
    for name, value in seed["featureFlags"].items():
        flag = flags.get(name, {})
        if (flag.get("effectiveValue"), flag.get("source")) != (value, "env"):
            drift.append(f"featureFlags.{name}: expected {value!r} from env, got {flag or None}")
    key = _ok(client.get(f"/api/keys/{_app_key_id(seed)}"))
    _compare("appKeyPolicy", seed["appKeyPolicy"], key, drift)
    connections = {c["id"]: c for c in _items(_ok(client.get("/api/providers")), "connections")}
    seeded = {c["id"] for c in seed["import"]["providerConnections"]}
    _compare_set("connections", seeded, set(connections), drift)
    for want in seed["import"]["providerConnections"]:
        if (got := connections.get(want["id"])) is not None:
            _compare(
                f"connections.{want['id']}.baseUrl",
                want["providerSpecificData"]["baseUrl"],
                (got.get("providerSpecificData") or {}).get("baseUrl"),
                drift,
            )
    combos = {c["name"]: c for c in _items(_ok(client.get("/api/combos")), "combos")}
    expected = {c["name"]: c for c in seed["import"]["combos"]}
    _compare_set("combos", set(expected), set(combos), drift)
    for name in expected.keys() & combos.keys():
        want = {k: expected[name][k] for k in ("strategy", "models", "config")}
        _compare(f"combos.{name}", want, combos[name], drift)
    for line in drift:
        logger.error("omniroute drift: %s", line)
    logger.info("omniroute verify: %d drift(s)", len(drift))
    return drift


def _compare(path: str, want: Any, got: Any, drift: list[str]) -> None:
    if isinstance(want, dict):
        if not isinstance(got, dict):
            drift.append(f"{path}: expected an object, got {got!r}")
            return
        for key, value in want.items():
            _compare(f"{path}.{key}", value, got.get(key), drift)
    elif isinstance(want, list) and isinstance(got, list):
        # Combo targets (dicts once stored) compare by their model; allowlists ignore order.
        got_names = [g["model"] if isinstance(g, dict) and "model" in g else g for g in got]
        if sorted(map(str, want)) != sorted(map(str, got_names)):
            drift.append(f"{path}: expected {want!r}, got {got!r}")
    elif want != got:
        drift.append(f"{path}: expected {want!r}, got {got!r}")


def _compare_set(path: str, want: set[str], got: set[str], drift: list[str]) -> None:
    if want != got:
        drift.append(f"{path}: expected exactly {sorted(want)}, got {sorted(got)}")


def _items(body: Any, key: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = body if isinstance(body, list) else body[key]
    return items


def _app_key_id(seed: dict[str, Any]) -> str:
    key_id: str = seed["import"]["apiKeys"][0]["id"]
    return key_id


def _ok(response: httpx.Response) -> Any:
    if response.is_error:
        raise SeedError(
            f"{response.request.method} {response.request.url.path} -> {response.status_code}"
        )
    return response.json()


def main() -> int:
    settings = load_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    args = sys.argv[1:]
    live = args[1:] == ["--live"]
    if args[:1] not in (["apply"], ["verify"]) or args[1:] not in ([], ["--live"]):
        logger.error("usage: omniroute_seed.py apply|verify [--live]")
        return 2
    if refusal := check_target(settings.gateway_base_url, live):
        logger.error("omniroute seed refused: %s", refusal)
        return 2
    seed = load_seed(live)
    logger.info(
        "omniroute %s: %s seed, gateway port %s",
        args[0],
        "live" if live else "stub",
        httpx.URL(settings.gateway_base_url).port,
    )
    if args[0] == "apply" and live and UNSET in json.dumps(seed):
        logger.error("live model unset (C5): seed.live.json still names %s", UNSET)
        return 2
    try:
        with admin_client(settings.gateway_base_url, admin_password()) as client:
            if sys.argv[1] == "apply":
                apply(client, seed, settings.gateway_api_key.get_secret_value())
            return 1 if verify(client, seed) else 0
    except SeedError as exc:
        logger.error("omniroute seed failed: %s", exc)
        return 1
    except httpx.TransportError as exc:  # str(exc) would carry the URL
        logger.error("omniroute unreachable: %s", type(exc).__name__)
        return 1


if __name__ == "__main__":
    sys.exit(main())
