from importlib.metadata import version
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from surakshasetu.api.app import create_app
from surakshasetu.config import Settings


def test_healthz_is_liveness_only() -> None:
    # Unreachable dependencies must not affect liveness.
    settings = Settings(
        _env_file=None,
        env="pilot",
        pg_dsn_app=SecretStr("postgresql://app@unreachable.invalid:5432/x"),
        redis_url=SecretStr("redis://unreachable.invalid:6379/0"),
        domain_base_url="http://unreachable.invalid:8080",
        domain_token=SecretStr("pilot-token"),
        kek_b64=SecretStr("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="),
        pg_dsn_keyvault=SecretStr("postgresql://keyvault@unreachable.invalid:5432/x"),
        minio_endpoint="https://unreachable.invalid:9000",
        minio_access_key=SecretStr("anchor-writer"),
        minio_secret_key=SecretStr("pilot-secret"),
        tsa_key_path=Path("/nonexistent/tsa.pem"),
        anchor_retention_days=3650,
    )
    client = TestClient(create_app(settings))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": version("surakshasetu")}


def test_every_response_carries_a_request_id_and_untrusted_ids_are_replaced() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    fresh = client.get("/healthz").headers["X-Request-ID"]
    echoed = client.get("/healthz", headers={"X-Request-ID": "req-abc.1_2"})
    bad_values = ("x" * 65, "evil id; level=CRITICAL")
    replaced = [client.get("/healthz", headers={"X-Request-ID": v}) for v in bad_values]

    assert len(fresh) == 32
    assert echoed.headers["X-Request-ID"] == "req-abc.1_2"
    for value, response in zip(bad_values, replaced, strict=True):
        assert response.headers["X-Request-ID"] not in (value, fresh)
        assert len(response.headers["X-Request-ID"]) == 32
