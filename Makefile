# One entry point for local work and CI. Placeholder targets are filled in by later steps.
COMPOSE := docker compose -f infra/compose.yaml --profile core
PYTEST_MARKERS := not stack and not golden and not redteam and not live
PLACEHOLDERS := db-migrate seed-catalog kb-ingest seed-eval test-invariants e2e-scripted \
	verify-audit verify-release-gate eval eval-live contracts contract-test local-setup

.PHONY: up down logs check check-py check-java check-stubs $(PLACEHOLDERS)

# `up --wait` treats an exited one-shot as a failure, so minio-init runs on its own afterwards.
up:
	$(COMPOSE) up -d --build --wait --scale minio-init=0
	$(COMPOSE) run --rm minio-init

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

check: check-py check-java check-stubs

check-py:
	cd orchestrator && uv run --locked ruff check && uv run --locked ruff format --check \
		&& uv run --locked mypy src && uv run --locked pytest -m "$(PYTEST_MARKERS)"

check-java:
	cd domain-services && ./mvnw -B verify

check-stubs:
	cd tools/stubs && uv run --locked ruff check && uv run --locked ruff format --check \
		&& uv run --locked pytest

$(PLACEHOLDERS):
	@echo "$@: not implemented yet"
