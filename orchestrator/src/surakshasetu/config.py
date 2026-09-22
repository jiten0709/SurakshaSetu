"""Runtime settings, read from SS_* environment variables and an optional .env file."""

from typing import Literal, Self

from pydantic import SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "SS_"
# Dependencies with no safe default outside a developer machine.
REQUIRED_OUTSIDE_DEV = ("pg_dsn_app", "redis_url")


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
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def _require_dependencies_outside_dev(self) -> Self:
        if self.env in ("pilot", "prod"):
            missing = [
                f"{ENV_PREFIX}{name.upper()}"
                for name in REQUIRED_OUTSIDE_DEV
                if getattr(self, name) is None
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
