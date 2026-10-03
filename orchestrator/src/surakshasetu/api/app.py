"""Conversation API application factory."""

import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from http import HTTPStatus
from importlib.metadata import version
from uuid import uuid4

import psycopg
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from surakshasetu.api.routes import router
from surakshasetu.config import Settings, load_settings
from surakshasetu.domain.client import DomainError
from surakshasetu.graph.runtime import ProblemError, Runtime
from surakshasetu.logging import configure_logging, request_id_ctx

logger = logging.getLogger(__name__)

VERSION = version("surakshasetu")
# The caller's X-Request-ID is untrusted: anything else gets a fresh id, so it can't inject lines.
_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
BODY_LIMIT = 16 * 1024


def problem(status: int, code: str | None = None) -> JSONResponse:
    """RFC 9457, as the domain tier answers: the contract `code`, never exception text."""
    return JSONResponse(
        {
            "type": "about:blank",
            "title": HTTPStatus(status).phrase,
            "status": status,
            "code": code or HTTPStatus(status).name,
        },
        status_code=status,
        media_type="application/problem+json",
    )


class BodyLimit:
    """413 once a request body passes BODY_LIMIT, declared or streamed. It raises from inside the
    app's receive, so FastAPI's exception handlers render the problem."""

    def __init__(self, app: ASGIApp, limit: int = BODY_LIMIT) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"0")
        received = 0

        async def limited() -> Message:
            nonlocal received
            if int(declared or b"0") > self.limit:
                raise ProblemError(413)
            message = await receive()
            received += len(message.get("body", b""))
            if received > self.limit:
                raise ProblemError(413)
            return message

        await self.app(scope, limited, send)


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    """`runtime` is for tests; otherwise the lifespan opens one from settings."""
    settings = settings or load_settings()
    configure_logging(settings.log_level, settings.log_dir, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if runtime is not None:
            app.state.runtime = runtime
            yield
            return
        async with Runtime.open(settings) as opened:
            app.state.runtime = opened
            yield

    app = FastAPI(title="SurakshaSetu Conversation API", version=VERSION, lifespan=lifespan)
    app.state.settings = settings
    if runtime is not None:
        app.state.runtime = runtime  # also without the lifespan (TestClient outside `with`)
    app.add_middleware(BodyLimit)

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

    @app.exception_handler(HTTPException)
    async def http_problem(request: Request, exc: HTTPException) -> Response:
        return problem(exc.status_code, getattr(exc, "code", None))

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:
        # Never echo the input: a turn body is customer text.
        return problem(400)

    async def unavailable(request: Request, exc: Exception) -> Response:
        logger.warning("dependency unavailable: %s", type(exc).__name__)
        return problem(503)

    app.add_exception_handler(RedisError, unavailable)
    app.add_exception_handler(psycopg.OperationalError, unavailable)

    @app.exception_handler(DomainError)
    async def domain_down(request: Request, exc: DomainError) -> Response:
        logger.warning("domain tier: %s", exc.code)
        return problem(503 if exc.status is None or exc.status >= 500 else 502)

    @app.exception_handler(Exception)
    async def crashed(request: Request, exc: Exception) -> Response:
        logger.exception("unhandled error")
        return problem(500)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """Liveness only: never touches a dependency."""
        return {"status": "ok", "version": VERSION}

    app.include_router(router)
    logger.info("app ready env=%s version=%s", settings.env, VERSION)
    return app
