"""Conversation API application factory."""

from importlib.metadata import version

from fastapi import FastAPI

from surakshasetu.config import Settings, load_settings
from surakshasetu.logging import configure_logging

VERSION = version("surakshasetu")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="SurakshaSetu Conversation API", version=VERSION)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only: never touches a dependency."""
        return {"status": "ok", "version": VERSION}

    return app
