# One entry point for local work and CI. Placeholder targets are filled in by later steps.
COMPOSE := docker compose -f infra/compose.yaml --profile core
PYTEST_MARKERS := not stack and not golden and not redteam and not live and not db
PLACEHOLDERS := kb-ingest seed-eval test-invariants e2e-scripted \
	verify-release-gate eval eval-live contract-test local-setup
SPEC := contracts/openapi/domain-services.v1.yaml
MODELS := src/surakshasetu/domain/models.py

# Host-side database steps use the same passwords as compose: infra/.env when present, otherwise
# the dummy defaults from infra/.env.example.
-include infra/.env
POSTGRES_PASSWORD ?= surakshasetu-dev
APP_RW_PASSWORD ?= surakshasetu-dev-app-rw
KEYVAULT_RW_PASSWORD ?= surakshasetu-dev-keyvault-rw
MINIO_ROOT_USER ?= surakshasetu
MINIO_ROOT_PASSWORD ?= surakshasetu-dev-minio
DATABASES := surakshasetu surakshasetu_test
MINIO_ENV := SS_MINIO_ACCESS_KEY="$(MINIO_ROOT_USER)" SS_MINIO_SECRET_KEY="$(MINIO_ROOT_PASSWORD)"
# What the db- and stack-marked tests connect with (tests/conftest.py).
TEST_ENV := SS_TEST_PG_DSN_ADMIN="postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_KEYVAULT="postgresql://keyvault_rw:$(KEYVAULT_RW_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	$(MINIO_ENV)

.PHONY: up down logs check check-py check-java check-stubs check-db check-stack check-contracts \
	db-migrate seed-catalog contracts contracts-lint verify-audit $(PLACEHOLDERS)

# Postgres first, then the migrations, so domain-services finds its domain_rw role on a fresh
# volume. `up --wait` treats an exited one-shot as a failure, so the one-shots run on their own.
up:
	$(COMPOSE) up -d --wait postgres
	@$(MAKE) --no-print-directory db-migrate
	$(COMPOSE) up -d --build --wait --scale minio-init=0 --scale flyway=0
	$(COMPOSE) run --rm minio-init

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f

check: check-py check-java check-stubs

check-py: check-contracts
	cd orchestrator && uv run --locked ruff check && uv run --locked ruff format --check \
		&& uv run --locked mypy src && uv run --locked pytest -m "$(PYTEST_MARKERS)"

contracts-lint:
	cd orchestrator && uv run --locked openapi-spec-validator ../$(SPEC)

# Regenerate the committed Python models; options live in orchestrator/pyproject.toml. The Java
# side regenerates on every Maven build, so only Python needs this.
contracts: contracts-lint
	cd orchestrator && uv run --locked datamodel-codegen --output $(MODELS)

# Drift check: regenerate to a temp file and diff it against the committed models. Generating
# to stdout keeps ruff on orchestrator's config wherever the temp file lives.
check-contracts: contracts-lint
	@cd orchestrator && tmp=$$(mktemp) && trap 'rm -f "$$tmp"' EXIT \
		&& uv run --locked datamodel-codegen > "$$tmp" && diff -u $(MODELS) "$$tmp" \
		|| { echo "check-contracts: $(MODELS) differs from $(SPEC); run make contracts" >&2; exit 1; }

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

# Load content/seed (catalog, disclosure registry, notices, reference masters) into surakshasetu
# with the service's own code (profile catalog-sync), then exit. Idempotent; needs `make up`.
seed-catalog:
	$(COMPOSE) run --rm --build -e SPRING_PROFILES_ACTIVE=catalog-sync domain-services

# The db-marked tests: privileges, the audit chain, subject keys. Needs `make up`; kept out of
# check-py because CI's python job has no database.
check-db: db-migrate
	@cd orchestrator && $(TEST_ENV) uv run --locked pytest -m db

# The stack-marked tests (the anchor against MinIO's object lock). Needs `make up`.
check-stack: db-migrate
	@cd orchestrator && $(TEST_ENV) uv run --locked pytest -m stack

# Verify every audit chain active on DATE (UTC) and anchor the day's Merkle root in MinIO and
# audit.chain_anchor, as app_rw. DB=surakshasetu_test checks the test database instead.
DB ?= surakshasetu
verify-audit:
	@test -n "$(DATE)" || { echo "usage: make verify-audit DATE=YYYY-MM-DD [DB=surakshasetu]" >&2; exit 2; }
	@cd orchestrator && $(MINIO_ENV) \
		SS_PG_DSN_APP="postgresql://app_rw:$(APP_RW_PASSWORD)@127.0.0.1:5432/$(DB)" \
		uv run --locked python -m surakshasetu.audit.verify --date "$(DATE)"

$(PLACEHOLDERS):
	@echo "$@: not implemented yet"
