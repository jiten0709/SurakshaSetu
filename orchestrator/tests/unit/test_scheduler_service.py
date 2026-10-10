"""The orchestrator image and its two compose services, profile app (Step 25): the scheduler runs
the timers module in the same image, the image runs as a non-root user with a liveness healthcheck
and carries the content a turn reads where the code looks for it, and both services get `make
serve`'s environment with service hostnames. Read from the files: starting them is `make app-up`.
"""

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import SecretStr

from surakshasetu.compose import bundle
from surakshasetu.config import Settings
from surakshasetu.graph import side_query
from surakshasetu.rails import output
from surakshasetu.retrieval import rewrite

REPO = Path(__file__).resolve().parents[3]
COMPOSE: dict[str, Any] = yaml.safe_load((REPO / "infra" / "compose.yaml").read_text("utf-8"))
SERVICES = COMPOSE["services"]
DOCKERFILE = (REPO / "orchestrator" / "Dockerfile").read_text("utf-8")
MAKEFILE = (REPO / "Makefile").read_text("utf-8")
# Settings with a 127.0.0.1 default that only host-side CLIs read: the dossier (compliance_ro) and
# verify-audit (MinIO). The API and the timers never do.
HOST_ONLY = {"pg_dsn_compliance", "minio_endpoint"}


def test_the_scheduler_runs_the_timers_in_the_orchestrator_image() -> None:
    api, scheduler = SERVICES["orchestrator"], SERVICES["scheduler"]
    command = scheduler["command"]

    assert api["profiles"] == scheduler["profiles"] == ["app"]
    assert scheduler["image"] == api["image"] and "build" not in scheduler
    assert command[:3] == ["python", "-m", "surakshasetu.jobs.timers"]
    assert command[command.index("--every") + 1] == "${SS_TIMER_INTERVAL_S:-60}"
    heartbeat = command[command.index("--heartbeat") + 1]
    probe = " ".join(scheduler["healthcheck"]["test"])
    assert heartbeat in probe and "SS_TIMER_INTERVAL_S" in probe
    assert scheduler["environment"] == api["environment"]
    assert api["environment"]["SS_TIMER_INTERVAL_S"] == "${SS_TIMER_INTERVAL_S:-60}"


def test_the_image_runs_non_root_with_a_liveness_healthcheck() -> None:
    users = re.findall(r"^USER (\S+)", DOCKERFILE, re.MULTILINE)
    healthcheck = re.search(r"^HEALTHCHECK (?:.*\\\n)*.*", DOCKERFILE, re.MULTILINE)

    assert users and users[-1] not in ("root", "0")
    assert "uv sync --frozen --no-dev" in DOCKERFILE
    assert healthcheck and "/healthz" in healthcheck.group(0)
    assert '"surakshasetu.api.app:create_app", "--factory"' in DOCKERFILE
    # Only the allowlisted files reach the build: never orchestrator/.env, .venv or .cache.
    rules = (REPO / "orchestrator" / ".dockerignore").read_text("utf-8").splitlines()
    rules = [rule for rule in rules if rule and not rule.startswith("#")]
    assert rules[0] == "*" and not [rule for rule in rules if ".env" in rule]


def test_the_image_carries_the_content_where_the_code_looks_for_it() -> None:
    # The code finds content/ at parents[4] of its own source; the image keeps the repo's layout.
    assert "WORKDIR /app/orchestrator" in DOCKERFILE
    assert SERVICES["orchestrator"]["build"]["additional_contexts"] == {"content": "../content"}
    for root in (bundle.PROMPT_BUNDLES, side_query.FAQ_ROOT, rewrite.KB_CONFIG, output.LEXICONS):
        relative = root.relative_to(REPO).as_posix()  # content/<dir>
        source = relative.removeprefix("content/")
        assert f"COPY --from=content {source} /app/{relative}" in DOCKERFILE


def app_env_names() -> set[str]:
    """The SS_* variables `make serve` exports (APP_ENV, with DOMAIN_ENV expanded)."""

    def block(name: str) -> str:
        found = re.search(rf"^{name} := ((?:.*\\\n)*.*)", MAKEFILE, re.MULTILINE)
        assert found, name
        return found.group(1)

    text = block("APP_ENV").replace("$(DOMAIN_ENV)", block("DOMAIN_ENV"))
    return set(re.findall(r"\b(SS_[A-Z0-9_]+)=", text))


def test_both_services_get_make_serves_environment_with_service_hostnames() -> None:
    env: dict[str, str] = SERVICES["orchestrator"]["environment"]
    names = app_env_names()

    assert {"SS_PG_DSN_APP", "SS_REDIS_URL", "SS_DOMAIN_TOKEN"} <= names <= set(env)
    assert not [value for value in env.values() if "127.0.0.1" in value or "localhost" in value]
    # Inside a container a 127.0.0.1 default points at the container itself.
    for name, field in Settings.model_fields.items():
        default = field.default
        text = default.get_secret_value() if isinstance(default, SecretStr) else str(default)
        if "127.0.0.1" in text and name not in HOST_ONLY:
            assert f"SS_{name.upper()}" in env, name
