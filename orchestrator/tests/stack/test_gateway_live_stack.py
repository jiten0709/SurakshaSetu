"""Design E (Step 1.5b, D-1) against the running omniroute-live. Only `make check-gateway-live` runs
it: it starts omniroute-live alone (--no-deps) before this module and removes it afterwards.

The module seeds omniroute-live itself with seed.json + seed.live.json, the overlay's model (C5b)
replaced by one no host has pulled (the owner's decision, 2026-11-14), so no Ollama is needed. The
payload rule then names the placeholder too, so the read-back proves it round-trips on 3.8.50.

The stub gateway (`omniroute`, :20130) and its volume (`omniroute-data`) are protected: this module
sends them no request, and never starts, stops, restarts, recreates or mounts anything. It reads
Docker's view of them (inspect, ps, volume ls/inspect) before the first test and after the last,
and shows nothing changed. A running OmniRoute writes its own database, so that content is not
compared; what is shown is that no path from the live run reaches it: no request goes to :20130,
the live instance's only upstreams are the stubs and the host's Ollama, it mounts nothing, and only
the stub container mounts the stub volume. A failed docker call fails the test; it is never read as
"absent".

Only fields that hold no secret are kept from `docker inspect` (never the environment).
"""

import asyncio
import importlib.util
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import uuid4

import httpx
import pytest

from surakshasetu.config import Settings
from surakshasetu.crypto.jcs import sha256_hex
from surakshasetu.gateway import (
    DataClass,
    Gateway,
    GatewayUnavailable,
    RedactionAttestation,
    Route,
)

pytestmark = pytest.mark.stack

REPO = Path(__file__).resolve().parents[3]
DOCKER = shutil.which("docker") or "docker"
COMPOSE = [DOCKER, "compose", "-f", str(REPO / "infra" / "compose.yaml")]
COMPOSE += ["--profile", "core", "--profile", "gateway", "--profile", "gateway-live"]
LIVE_URL = "http://127.0.0.1:20131/v1"
STUBS_URL = os.environ.get("SS_TEST_STUBS_URL", "http://127.0.0.1:8090")
SETTINGS = Settings(_env_file=None, gateway_base_url=LIVE_URL)
PLACEHOLDER = "ss-check-never-pulled"  # a model name no Ollama host has
STUB_VOLUME = "surakshasetu_omniroute-data"
LIVE_UPSTREAMS = {"http://stubs:8090/v1", "http://host.docker.internal:11434/v1"}


def _load_seed_script() -> ModuleType:
    path = REPO / "orchestrator" / "scripts" / "omniroute_seed.py"
    spec = importlib.util.spec_from_file_location("omniroute_seed", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SEED_SCRIPT = _load_seed_script()


def _docker(*args: str) -> subprocess.CompletedProcess[str]:
    """A read-only docker CLI call with this module's fixed arguments (no outside input)."""
    return subprocess.run(list(args), capture_output=True, text=True, check=False)  # noqa: S603


def _lines(*args: str) -> list[str]:
    """The call's output lines; a failed call fails the test (never an empty answer)."""
    run = _docker(*args)
    assert run.returncode == 0, f"docker {args[1]} exited {run.returncode}"  # no output: no secrets
    return run.stdout.split()


def _time(value: str) -> float:
    """Docker's RFC 3339 time (nanoseconds, Z) as epoch seconds."""
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", value).replace("Z", "+00:00")
    return datetime.fromisoformat(trimmed).timestamp()


def _container(service: str) -> dict[str, Any] | None:
    """Docker's view of the service's container, without secrets, or None when it has none."""
    ids = _lines(*COMPOSE, "ps", "--all", "--quiet", service)
    if not ids:
        return None
    assert len(ids) == 1, f"{service}: {len(ids)} containers"
    run = _docker(DOCKER, "inspect", ids[0])
    assert run.returncode == 0, f"docker inspect ({service}) exited {run.returncode}"
    info = json.loads(run.stdout)[0]
    bindings = info["HostConfig"]["PortBindings"] or {}
    return {
        "id": info["Id"],
        "service": info["Config"]["Labels"].get("com.docker.compose.service"),
        "created": _time(info["Created"]),
        "started": _time(info["State"]["StartedAt"]),
        "restarts": info["RestartCount"],
        "running": info["State"]["Running"],
        "ports": {
            port: sorted((b["HostIp"], b["HostPort"]) for b in binds or [])
            for port, binds in bindings.items()
        },
        "mounts": sorted(
            (m["Type"], m.get("Name", ""), m["Destination"], m["RW"]) for m in info["Mounts"]
        ),
        "bound": "OMNIROUTE_DIRECT_HEADERS_TIMEOUT_MS=105000" in info["Config"]["Env"],
    }


def _stub_volume() -> dict[str, Any] | None:
    """The stub volume's creation time and the containers that mount it, or None when Docker lists
    no such volume. Listing and inspecting never create a volume."""
    listed = _lines(DOCKER, "volume", "ls", "--quiet", "--filter", f"name={STUB_VOLUME}")
    if STUB_VOLUME not in listed:
        return None
    created = _lines(DOCKER, "volume", "inspect", "--format", "{{.CreatedAt}}", STUB_VOLUME)
    users = _lines(
        DOCKER, "ps", "--all", "--no-trunc", "--filter", f"volume={STUB_VOLUME}",
        "--format", "{{.ID}}",
    )  # fmt: skip
    return {"created": " ".join(created), "users": sorted(users)}


def _protected() -> dict[str, Any]:
    return {
        "omniroute": _container("omniroute"),
        "stubs": _container("stubs"),
        "volume": _stub_volume(),
    }


@pytest.fixture(scope="module", autouse=True)
def before() -> dict[str, Any]:
    """The protected infrastructure as Docker sees it, before the first test. The stubs must
    already be running: omniroute-live is started with --no-deps, so nothing here starts them."""
    try:
        stubs = httpx.get(f"{STUBS_URL}/healthz", timeout=3).status_code
    except httpx.TransportError:
        stubs = None
    assert stubs == 200, "the stubs are not running: start them (make up) before this module"
    return _protected()


@pytest.fixture(scope="module")
def live() -> dict[str, Any]:
    found = _container("omniroute-live")
    assert found is not None and found["running"], "run this module with make check-gateway-live"
    return found


@pytest.fixture(scope="module")
def seeded() -> dict[str, Any]:
    """The merged live seed with the placeholder model, applied to :20131 and read back."""
    assert SEED_SCRIPT.check_target(LIVE_URL, live=True) is None
    live = SEED_SCRIPT.load_seed(live=True)
    model = live["payloadRules"]["default"][0]["models"][0]["name"]
    seed: dict[str, Any] = json.loads(json.dumps(live).replace(model, PLACEHOLDER))
    assert model not in json.dumps(seed) and SEED_SCRIPT.UNSET not in json.dumps(seed)
    with SEED_SCRIPT.admin_client(LIVE_URL, SEED_SCRIPT.admin_password()) as admin:
        SEED_SCRIPT.apply(admin, seed, SETTINGS.gateway_api_key.get_secret_value())
        assert SEED_SCRIPT.verify(admin, seed) == []
    return seed


def test_every_address_here_is_the_live_gateway_or_the_stubs() -> None:
    assert httpx.URL(SETTINGS.gateway_base_url).port == 20131
    assert httpx.URL(STUBS_URL).port == 8090
    assert SEED_SCRIPT.check_target("http://127.0.0.1:20130/v1", live=True) is not None


def test_omniroute_live_listens_on_20131_and_mounts_nothing(live: dict[str, Any]) -> None:
    assert live["service"] == "omniroute-live"
    assert live["ports"] == {"20128/tcp": [("127.0.0.1", "20131")]}
    assert live["mounts"] == []  # no persistent volume: its database dies with the container
    assert live["bound"]  # O2/D-2: 105 s


def test_the_stub_gateway_stays_on_20130_with_its_own_volume(before: dict[str, Any]) -> None:
    stub = before["omniroute"]
    if stub is None:
        pytest.skip("no stub omniroute container exists; test_omniroute_hardening pins it")
    assert stub["service"] == "omniroute"
    assert stub["ports"] == {"20128/tcp": [("127.0.0.1", "20130")]}
    assert stub["mounts"] == [("volume", STUB_VOLUME, "/app/data", True)]


def test_the_live_seed_applies_and_reads_back_with_no_drift(seeded: dict[str, Any]) -> None:
    connections = seeded["import"]["providerConnections"]
    upstreams = {c["providerSpecificData"]["baseUrl"] for c in connections}
    assert upstreams == LIVE_UPSTREAMS  # never the stub gateway (omniroute:20128, :20130)
    targets = {c["name"]: c["models"] for c in seeded["import"]["combos"]}
    assert targets["guard-input"] == [f"ollama/{PLACEHOLDER}"]
    assert targets["verify-claims"] == [f"ollama-local/{PLACEHOLDER}"]
    assert targets["gen-converse"] == ["vllm/stub-gen"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_gen_converse_reaches_the_stubs() -> None:
    session_id = uuid4()
    messages = [{"role": "user", "content": "Hello"}]
    async with Gateway(SETTINGS) as gateway:
        result = await gateway.call(
            Route.GEN_CONVERSE,
            data_class=DataClass.REDACTED,
            messages=messages,
            session_id=session_id,
            turn_id=uuid4(),
            fsm_state="S1",
            attestation=RedactionAttestation(sha256_hex(messages)),
        )
    async with httpx.AsyncClient(base_url=STUBS_URL) as stubs:
        arrived = (await stubs.get(f"/__last/{session_id}")).status_code

    assert result.served_model == "stub-gen"
    assert arrived == 200  # the existing stubs received it


@pytest.mark.asyncio
@pytest.mark.usefixtures("seeded")
async def test_a_self_hosted_route_fails_closed_without_fallback() -> None:
    # Wider budgets, so the gateway's own failure (no Ollama, or no such model) is what returns.
    session_id = uuid4()
    async with Gateway(SETTINGS.model_copy(update={"gateway_timeout_scale": 15})) as gateway:
        with pytest.raises(GatewayUnavailable):
            await gateway.call(
                Route.GUARD_INPUT,
                data_class=DataClass.SELF_HOSTED_RAW,
                messages=[{"role": "user", "content": "Hello"}],
                session_id=session_id,
                turn_id=uuid4(),
                fsm_state="S1",
            )
    await asyncio.sleep(2)  # anything a fallback would send has arrived by now
    async with httpx.AsyncClient(base_url=STUBS_URL) as stubs:
        assert (await stubs.get(f"/__last/{session_id}")).status_code == 404


def test_the_protected_infrastructure_is_unchanged(
    before: dict[str, Any], live: dict[str, Any]
) -> None:
    after = _protected()

    # The same containers in the same state: none started, stopped, restarted or recreated, and
    # the stub volume neither recreated nor mounted by anything new.
    assert after == before
    for service in ("omniroute", "stubs"):
        if (container := before[service]) is not None:
            assert container["created"] < live["created"]  # not recreated by the live start
            assert container["started"] < live["created"]  # not (re)started by it
    if (volume := before["volume"]) is not None:
        stub = before["omniroute"]
        assert volume["users"] == ([stub["id"]] if stub else [])  # only the stub mounts it
