FROM python:3.12.14-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/app/orchestrator/.venv/bin:$PATH
# The repository's layout: the code finds content/ four levels above its own source files
# (Path(__file__).resolve().parents[4]), so the project stays an editable install under
# /app/orchestrator and the runtime's content sits at /app/content.
WORKDIR /app/orchestrator
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY src src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
# The named context comes from infra/compose.yaml. Only what a turn reads: prompt bundles, the
# privacy FAQ, the retrieval config and thresholds, the output lexicon packs.
COPY --from=content prompt-bundles /app/content/prompt-bundles
COPY --from=content faq /app/content/faq
COPY --from=content kb /app/content/kb
COPY --from=content lexicon /app/content/lexicon
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app
EXPOSE 8000
# Liveness only; /readyz is for whoever routes traffic. The scheduler overrides this in compose.
HEALTHCHECK --interval=5s --timeout=5s --start-period=60s --retries=30 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"]
CMD ["uvicorn", "surakshasetu.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
