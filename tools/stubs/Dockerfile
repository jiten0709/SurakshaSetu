FROM python:3.12.14-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY *.py ./
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin stubs
USER stubs
EXPOSE 8090
CMD [".venv/bin/uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8090"]
