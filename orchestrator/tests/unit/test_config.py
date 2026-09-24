import os
from pathlib import Path

import pytest

from surakshasetu.config import ConfigError, load_settings


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate from the developer's shell and any .env in the working directory."""
    for name in list(os.environ):
        if name.startswith("SS_"):
            monkeypatch.delenv(name)
    monkeypatch.chdir(tmp_path)


def test_dev_loads_without_dependencies_configured() -> None:
    settings = load_settings()
    assert settings.env == "dev"
    assert settings.pg_dsn_app is None


def test_pilot_with_missing_dsn_fails_readably(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_ENV", "pilot")
    monkeypatch.setenv("SS_REDIS_URL", "redis://:s3cret@valkey:6379/0")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    message = str(excinfo.value)
    assert "SS_PG_DSN_APP" in message
    assert "SS_REDIS_URL" not in message
    assert "s3cret" not in message


def test_empty_value_counts_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_ENV", "prod")
    monkeypatch.setenv("SS_PG_DSN_APP", "")

    with pytest.raises(ConfigError, match="SS_PG_DSN_APP, SS_REDIS_URL"):
        load_settings()


def test_dev_domain_token_default_is_refused_outside_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load_settings().domain_token.get_secret_value()

    monkeypatch.setenv("SS_ENV", "pilot")
    monkeypatch.setenv("SS_PG_DSN_APP", "postgresql://app_rw:x@postgres:5432/surakshasetu")
    monkeypatch.setenv("SS_REDIS_URL", "redis://valkey:6379/0")

    with pytest.raises(ConfigError, match="requires SS_DOMAIN_TOKEN$"):
        load_settings()

    monkeypatch.setenv("SS_DOMAIN_TOKEN", "pilot-token")
    assert load_settings().domain_token.get_secret_value() == "pilot-token"


def test_invalid_env_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_ENV", "staging")

    with pytest.raises(ConfigError, match="SS_ENV"):
        load_settings()
