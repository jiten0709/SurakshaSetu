# One entry point for local work and CI. Placeholder targets are filled in by later steps.
COMPOSE := docker compose -f infra/compose.yaml --profile core
PYTEST_MARKERS := not stack and not golden and not redteam and not live and not db
PLACEHOLDERS := seed-catalog kb-ingest seed-eval test-invariants e2e-scripted \
	verify-audit verify-release-gate eval eval-live contracts contract-test local-setup

# Host-side database steps use the same passwords as compose: infra/.env when present, otherwise
# the dummy defaults from infra/.env.example.
-include infra/.env
POSTGRES_PASSWORD ?= surakshasetu-dev
APP_RW_PASSWORD ?= surakshasetu-dev-app-rw
DATABASES := surakshasetu surakshasetu_test

.PHONY: up down logs check check-py check-java check-stubs check-db db-migrate $(PLACEHOLDERS)

# `up --wait` treats an exited one-shot as a failure, so the one-shots run on their own.
up:
	$(COMPOSE) up -d --build --wait --scale minio-init=0 --scale flyway=0
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

# Flyway per database, then the LangGraph checkpointer creates its tables in langgraph as app_rw.
# Silent (@) so passwords from infra/.env never reach the terminal.
db-migrate:
	@for db in $(DATABASES); do \
		echo "db-migrate: $$db"; \
		$(COMPOSE) run --rm -e FLYWAY_URL=jdbc:postgresql://postgres:5432/$$db flyway migrate \
			|| exit 1; \
		(cd orchestrator && SS_PG_DSN_APP="postgresql://app_rw:$(APP_RW_PASSWORD)@127.0.0.1:5432/$$db" \
			uv run --locked python -m surakshasetu.graph.checkpointer_setup) || exit 1; \
	done

# The db-marked privilege tests. Needs `make up`; kept out of check-py because CI's python job
# has no database.
check-db: db-migrate
	@cd orchestrator && \
		SS_TEST_PG_DSN_ADMIN="postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
		uv run --locked pytest -m db

$(PLACEHOLDERS):
	@echo "$@: not implemented yet"
