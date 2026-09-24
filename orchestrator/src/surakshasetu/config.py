"""Runtime settings, read from SS_* environment variables and an optional .env file."""

from pathlib import Path
from typing import Literal, Self

from pydantic import SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "SS_"
# Settings with no safe default outside a developer machine: pilot and prod must set them.
REQUIRED_OUTSIDE_DEV = ("pg_dsn_app", "redis_url", "domain_token")


class ConfigError(RuntimeError):
    """The environment does not form valid Settings. The message never contains values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        hide_input_in_errors=True,
    )

    env: Literal["dev", "test", "pilot", "prod"] = "dev"
    pg_dsn_app: SecretStr | None = None
    redis_url: SecretStr | None = None
    domain_base_url: str = "http://127.0.0.1:8080"
    domain_token: SecretStr = SecretStr("surakshasetu-dev-domain-token")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Opt-in local extras: per-subsystem JSON files, and a readable console instead of JSON.
    log_dir: Path | None = None
    log_format: Literal["json", "text"] = "json"

    @model_validator(mode="after")
    def _require_dependencies_outside_dev(self) -> Self:
        if self.env in ("pilot", "prod"):
            missing = [
                f"{ENV_PREFIX}{name.upper()}"
                for name in REQUIRED_OUTSIDE_DEV
                if name not in self.model_fields_set or getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"env={self.env} requires {', '.join(missing)}")
        return self


def load_settings() -> Settings:
    """Load settings, failing fast with one readable line per problem."""
    try:
        return Settings()
    except ValidationError as exc:
        problems = "\n".join(
            f"  {_variable(err['loc'])}: {err['msg']}"
            for err in exc.errors(include_url=False, include_input=False)
        )
        raise ConfigError(f"invalid configuration:\n{problems}") from None


def _variable(loc: tuple[int | str, ...]) -> str:
    """Map a pydantic error location to the environment variable a human would set."""
    return f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else "settings"
