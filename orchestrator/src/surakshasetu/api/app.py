"""Conversation API application factory."""

import logging
import re
from collections.abc import Awaitable, Callable
from importlib.metadata import version
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from surakshasetu.config import Settings, load_settings
from surakshasetu.logging import configure_logging, request_id_ctx

logger = logging.getLogger(__name__)

VERSION = version("surakshasetu")
# The caller's X-Request-ID is untrusted: anything else gets a fresh id, so it can't inject lines.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)
    app = FastAPI(title="SurakshaSetu Conversation API", version=VERSION)

    @app.middleware("http")
    async def bind_request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID.fullmatch(incoming) else uuid4().hex
        token = request_id_ctx.set(request_id)
        try:
            response = await call_next(request)
        finally:
            request_id_ctx.reset(token)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only: never touches a dependency."""
        return {"status": "ok", "version": VERSION}

    logger.info("app ready env=%s version=%s", settings.env, VERSION)
    return app
