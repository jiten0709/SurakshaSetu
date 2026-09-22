from importlib.metadata import version

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
    )
    client = TestClient(create_app(settings))

    response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": version("surakshasetu")}
