import base64
import os
from pathlib import Path

import pytest

from surakshasetu.config import ConfigError, load_settings

# Step 4's keys and anchoring: dev has dummy defaults, pilot and prod must set every one.
KEY_AND_ANCHOR_ENV = {
    "SS_KEK_B64": base64.b64encode(bytes(32)).decode(),
    "SS_PG_DSN_KEYVAULT": "postgresql://keyvault_rw:x@postgres:5432/surakshasetu",
    "SS_MINIO_ENDPOINT": "https://minio.internal:9000",
    "SS_MINIO_ACCESS_KEY": "anchor-writer",
    "SS_MINIO_SECRET_KEY": "s3cret",
    "SS_TSA_KEY_PATH": "/run/secrets/tsa.pem",
    "SS_ANCHOR_RETENTION_DAYS": "3650",
}
# Step 8's model path: the dev defaults point at local containers and a dummy key.
GATEWAY_ENV = {
    "SS_GATEWAY_BASE_URL": "https://omniroute.internal/v1",
    "SS_GATEWAY_API_KEY": "gateway-key",
    "SS_TEI_EMBED_URL": "http://tei-embed.internal",
    "SS_TEI_RERANK_URL": "http://tei-rerank.internal",
}
# Step 11's knowledge base.
KB_ENV = {"SS_QDRANT_URL": "http://qdrant.internal:6333"}


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
    assert len(base64.b64decode(settings.kek_b64.get_secret_value())) == 32
    assert settings.tsa_key_path is None


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
    for name, value in (KEY_AND_ANCHOR_ENV | GATEWAY_ENV | KB_ENV).items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match="requires SS_DOMAIN_TOKEN$"):
        load_settings()

    monkeypatch.setenv("SS_DOMAIN_TOKEN", "pilot-token")
    assert load_settings().domain_token.get_secret_value() == "pilot-token"


def test_pilot_requires_every_key_and_anchor_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_ENV", "pilot")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    for name in KEY_AND_ANCHOR_ENV:
        assert name in str(excinfo.value)


def test_pilot_requires_every_model_path_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_ENV", "pilot")

    with pytest.raises(ConfigError) as excinfo:
        load_settings()

    for name in GATEWAY_ENV | KB_ENV:
        assert name in str(excinfo.value)
    assert "SS_EMBED_DIM" not in str(excinfo.value)


def test_model_path_dev_defaults_are_the_local_stack() -> None:
    settings = load_settings()

    # compose's OmniRoute (profile gateway), not the stubs behind it
    assert settings.gateway_base_url == "http://127.0.0.1:20130/v1"
    assert (settings.tei_embed_url, settings.tei_rerank_url) == (
        "http://127.0.0.1:8081",
        "http://127.0.0.1:8082",
    )
    assert (settings.embed_dim, settings.embed_query_prefix) == (1024, "")
    assert settings.qdrant_url == "http://127.0.0.1:6333"


def test_invalid_env_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_ENV", "staging")

    with pytest.raises(ConfigError, match="SS_ENV"):
        load_settings()


@pytest.mark.parametrize("env", ["pilot", "prod"])
def test_the_tei_timeout_scale_is_dev_and_test_only(
    monkeypatch: pytest.MonkeyPatch, env: str
) -> None:
    monkeypatch.setenv("SS_TEI_TIMEOUT_SCALE", "400")
    assert load_settings().tei_timeout_scale == 400

    monkeypatch.setenv("SS_ENV", env)
    monkeypatch.setenv("SS_PG_DSN_APP", "postgresql://app_rw:x@postgres:5432/surakshasetu")
    monkeypatch.setenv("SS_REDIS_URL", "redis://valkey:6379/0")
    monkeypatch.setenv("SS_DOMAIN_TOKEN", "pilot-token")
    for name, value in (KEY_AND_ANCHOR_ENV | GATEWAY_ENV | KB_ENV).items():
        monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match="SS_TEI_TIMEOUT_SCALE"):
        load_settings()

    monkeypatch.setenv("SS_TEI_TIMEOUT_SCALE", "1")
    assert load_settings().tei_timeout_scale == 1


def test_the_tei_timeout_scale_cannot_shrink_a_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SS_TEI_TIMEOUT_SCALE", "0.5")

    with pytest.raises(ConfigError, match="SS_TEI_TIMEOUT_SCALE"):
        load_settings()


def test_the_output_rail_settings_have_safe_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    assert (settings.output_lexicon, settings.verify_sample_rate) == ("2026.09.1", 0.1)

    monkeypatch.setenv("SS_OUTPUT_LEXICON", "2026.10.2")
    monkeypatch.setenv("SS_VERIFY_SAMPLE_RATE", "1")
    assert (load_settings().output_lexicon, load_settings().verify_sample_rate) == ("2026.10.2", 1)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SS_VERIFY_SAMPLE_RATE", "1.5"),
        ("SS_VERIFY_SAMPLE_RATE", "-0.1"),
        ("SS_OUTPUT_LEXICON", "../2026.09.1"),
        ("SS_OUTPUT_LEXICON", "latest"),
    ],
)
def test_the_output_rail_settings_refuse_bad_values(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ConfigError, match=name):
        load_settings()
