# One entry point for local work and CI. Placeholder targets are filled in by later steps.
COMPOSE := docker compose -f infra/compose.yaml --profile core
PYTEST_MARKERS := not stack and not golden and not redteam and not live and not db
PLACEHOLDERS := seed-eval test-invariants e2e-scripted \
	verify-release-gate eval eval-live local-setup
SPEC := contracts/openapi/domain-services.v1.yaml
MODELS := src/surakshasetu/domain/models.py

# Host-side database steps use the same passwords as compose: infra/.env when present, otherwise
# the dummy defaults from infra/.env.example.
-include infra/.env
POSTGRES_PASSWORD ?= surakshasetu-dev
APP_RW_PASSWORD ?= surakshasetu-dev-app-rw
KEYVAULT_RW_PASSWORD ?= surakshasetu-dev-keyvault-rw
CATALOG_LOADER_PASSWORD ?= surakshasetu-dev-catalog-loader
MINIO_ROOT_USER ?= surakshasetu
MINIO_ROOT_PASSWORD ?= surakshasetu-dev-minio
OMNIROUTE_INITIAL_PASSWORD ?= surakshasetu-dev-omniroute
DATABASES := surakshasetu surakshasetu_test
# The knowledge base's corpus snapshots live in the dev database; retrieval reads them as app_rw.
PG_DSN_KB := postgresql://app_rw:$(APP_RW_PASSWORD)@127.0.0.1:5432/surakshasetu
MINIO_ENV := SS_MINIO_ACCESS_KEY="$(MINIO_ROOT_USER)" SS_MINIO_SECRET_KEY="$(MINIO_ROOT_PASSWORD)"
# What the db- and stack-marked tests connect with (tests/conftest.py).
TEST_ENV := SS_TEST_PG_DSN_ADMIN="postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_KEYVAULT="postgresql://keyvault_rw:$(KEYVAULT_RW_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_CATALOG_LOADER="postgresql://catalog_loader:$(CATALOG_LOADER_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_KB="$(PG_DSN_KB)" \
	$(MINIO_ENV)

.PHONY: up down logs check check-py check-java check-stubs check-db check-stack check-contracts \
	db-migrate seed-catalog contracts contracts-lint contract-test verify-audit gateway-up \
	gateway-verify kb-ingest kb-verify kb-chunks check-ingest calibrate-retrieval eval-retrieval \
	bakeoff-embed bakeoff-rerank $(PLACEHOLDERS)

# Postgres first, then the migrations, so domain-services finds its domain_rw role on a fresh
# volume. `up --wait` treats an exited one-shot as a failure, so the one-shots run on their own.
up:
	$(COMPOSE) up -d --wait postgres
	@$(MAKE) --no-print-directory db-migrate
	$(COMPOSE) up -d --build --wait --scale minio-init=0 --scale flyway=0
	$(COMPOSE) run --rm minio-init

down:
	$(COMPOSE) --profile gateway down

# SurakshaSetu's hardened OmniRoute (profile gateway, 127.0.0.1:20130), in front of the stubs for
# the chat routes: start it, then apply infra/omniroute/seed.json and read every value back.
# Idempotent. Separate from `up`, which never needs it.
SEED := cd orchestrator && OMNIROUTE_INITIAL_PASSWORD="$(OMNIROUTE_INITIAL_PASSWORD)" \
	SS_LOG_FORMAT=text uv run --locked python scripts/omniroute_seed.py
gateway-up:
	$(COMPOSE) --profile gateway up -d --build --wait omniroute
	@$(SEED) apply

# Exit 1 if any seeded value has drifted (every drift is logged).
gateway-verify:
	@$(SEED) verify

logs:
	$(COMPOSE) logs -f

check: check-py check-java check-stubs

check-py: check-contracts
	cd orchestrator && uv run --locked ruff check && uv run --locked ruff format --check \
		&& uv run --locked mypy src/surakshasetu && uv run --locked pytest -m "$(PYTEST_MARKERS)"

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

# Knowledge base (Step 11), with the ingest dependency group (docling, dagster, qdrant-client; torch
# comes with docling), which check-py and CI's python job never install. kb-ingest runs the Dagster
# assets over content/seed/kb into Qdrant and catalog.corpus_snapshot (as catalog_loader);
# idempotent per snapshot id. kb-verify checks the result and the golden sets. kb-chunks lists the
# approved chunk ids, for relabelling golden sets deliberately. All need `make up`.
INGEST := cd orchestrator && SS_LOG_FORMAT=text \
	SS_PG_DSN_CATALOG_LOADER="postgresql://catalog_loader:$(CATALOG_LOADER_PASSWORD)@127.0.0.1:5432/surakshasetu" \
	uv run --locked --group ingest python -m surakshasetu_ingest
kb-ingest:
	@$(INGEST) ingest

kb-verify:
	@$(INGEST) verify

kb-chunks:
	@$(INGEST) chunks

# mypy on the ingest package, then tests/ingest (the stack-marked one writes to its own Qdrant
# collections and surakshasetu_test, and cleans up). Needs `make up`.
check-ingest: db-migrate
	@cd orchestrator && uv run --locked --group ingest mypy src/surakshasetu_ingest \
		&& $(TEST_ENV) SS_TEST_INGEST=1 uv run --locked --group ingest pytest tests/ingest

# Retrieval (Step 12): the golden sets through the real pipeline (Qdrant, TEI, the dev catalog as
# app_rw). calibrate-retrieval picks the sufficiency thresholds and writes content/kb/thresholds.yaml;
# eval-retrieval prints the metrics and exits 1 below the gates (recall@8 >= 0.90, precision@8 >=
# 0.80, abstention >= 0.95). CPU TEI misses the GPU budgets, so both scale the embed and rerank
# timeouts (dev and test only; pilot and prod refuse it). Rerank scores are cached in
# orchestrator/.cache/rerank, so only the first run is slow (about an hour on CPU). Need `make up`
# and `make kb-ingest`.
EVAL := cd orchestrator && SS_LOG_FORMAT=text SS_TEI_TIMEOUT_SCALE=1000 SS_PG_DSN_APP="$(PG_DSN_KB)" \
	uv run --locked python -m surakshasetu.eval.retrieval_metrics
calibrate-retrieval:
	@$(EVAL) calibrate

eval-retrieval:
	@$(EVAL) eval

# The Step 12 bake-off (guide 7b; the human makes the call): the challenger TEI services run in
# profile bakeoff (:8083 embed, :8084 rerank), pinned in infra/compose.yaml. bakeoff-embed indexes
# the approved chunks with the challenger embedder into bakeoff_kb_* collections (no catalog row)
# and compares first-stage recall; bakeoff-rerank reranks one candidate pool with both rerankers
# (hours on CPU). Each downloads its model on first start.
bakeoff-embed:
	$(COMPOSE) --profile bakeoff up -d --wait tei-embed-challenger
	@export SS_TEI_TIMEOUT_SCALE=1000 && $(INGEST) bakeoff-embed

bakeoff-rerank:
	$(COMPOSE) --profile bakeoff up -d --wait tei-rerank-challenger
	@$(EVAL) bakeoff-rerank

# The db-marked tests: privileges, the audit chain, subject keys. Needs `make up`; kept out of
# check-py because CI's python job has no database.
check-db: db-migrate
	@cd orchestrator && $(TEST_ENV) uv run --locked pytest -m db

# Schemathesis against the running domain-services (SS_DOMAIN_BASE_URL, default :8080): every
# operation and the spec's links, checking not_a_server_error and response_schema_conformance.
# Needs `make up`. Consent records it creates land in the dev database.
contract-test:
	cd orchestrator && uv run --locked pytest -m stack tests/stack/test_domain_contract.py

# The stack-marked tests: the anchor against MinIO's object lock and the Schemathesis contract
# tests. Needs `make up`.
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
