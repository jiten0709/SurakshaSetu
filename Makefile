# One entry point for local work and CI. Placeholder targets are filled in by later steps.
COMPOSE := docker compose -f infra/compose.yaml --profile core
PYTEST_MARKERS := not stack and not golden and not redteam and not live and not db
PLACEHOLDERS :=
SPEC := contracts/openapi/domain-services.v1.yaml
CONV_SPEC := contracts/openapi/conversation-api.v1.yaml
MODELS := src/surakshasetu/domain/models.py

# Host-side database steps use the same passwords as compose: infra/.env when present, otherwise
# the dummy defaults from infra/.env.example.
-include infra/.env
POSTGRES_PASSWORD ?= surakshasetu-dev
APP_RW_PASSWORD ?= surakshasetu-dev-app-rw
KEYVAULT_RW_PASSWORD ?= surakshasetu-dev-keyvault-rw
CATALOG_LOADER_PASSWORD ?= surakshasetu-dev-catalog-loader
ERASURE_RW_PASSWORD ?= surakshasetu-dev-erasure-rw
COMPLIANCE_RO_PASSWORD ?= surakshasetu-dev-compliance-ro
DOMAIN_TOKEN ?= surakshasetu-dev-domain-token
DOMAIN_INTERNAL_TOKEN ?= surakshasetu-dev-domain-internal-token
MINIO_ROOT_USER ?= surakshasetu
MINIO_ROOT_PASSWORD ?= surakshasetu-dev-minio
OMNIROUTE_INITIAL_PASSWORD ?= surakshasetu-dev-omniroute
DATABASES := surakshasetu surakshasetu_test
# The knowledge base's corpus snapshots live in the dev database; retrieval reads them as app_rw.
PG_DSN_KB := postgresql://app_rw:$(APP_RW_PASSWORD)@127.0.0.1:5432/surakshasetu
MINIO_ENV := SS_MINIO_ACCESS_KEY="$(MINIO_ROOT_USER)" SS_MINIO_SECRET_KEY="$(MINIO_ROOT_PASSWORD)"
# domain-services' tokens as compose starts it (infra/.env, else the dummy defaults).
DOMAIN_ENV := SS_DOMAIN_TOKEN="$(DOMAIN_TOKEN)" SS_DOMAIN_INTERNAL_TOKEN="$(DOMAIN_INTERNAL_TOKEN)"
# What the db- and stack-marked tests connect with (tests/conftest.py).
TEST_ENV := SS_TEST_PG_DSN_ADMIN="postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_KEYVAULT="postgresql://keyvault_rw:$(KEYVAULT_RW_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_CATALOG_LOADER="postgresql://catalog_loader:$(CATALOG_LOADER_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_KB="$(PG_DSN_KB)" \
	SS_TEST_PG_DSN_APP="postgresql://app_rw:$(APP_RW_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	SS_TEST_PG_DSN_ERASURE="postgresql://erasure_rw:$(ERASURE_RW_PASSWORD)@127.0.0.1:5432/surakshasetu_test" \
	$(MINIO_ENV) $(DOMAIN_ENV)

.PHONY: up down logs check check-py check-java check-stubs check-db check-stack check-contracts \
	db-migrate seed-catalog contracts contracts-lint contract-test verify-audit gateway-up \
	gateway-verify kb-ingest kb-verify kb-chunks check-ingest calibrate-retrieval eval-retrieval \
	bakeoff-embed bakeoff-rerank test-invariants eval eval-live seed-eval e2e-scripted \
	timers-once serve local-setup verify-release-gate dossier app-up $(PLACEHOLDERS)

# Postgres first, then the migrations, so domain-services finds its domain_rw role on a fresh
# volume. `up --wait` treats an exited one-shot as a failure, so the one-shots run on their own.
up:
	$(COMPOSE) up -d --wait postgres
	@$(MAKE) --no-print-directory db-migrate
	$(COMPOSE) up -d --build --wait --scale minio-init=0 --scale flyway=0
	$(COMPOSE) run --rm minio-init

down:
	$(COMPOSE) --profile gateway --profile app down

# Step 25: the orchestrator image in compose (profile app): the Conversation API on 127.0.0.1:8000
# (the port `make serve` uses, so run one of the two) and the `scheduler` running the inactivity
# timers. Needs `make up`, and `make gateway-up` for GET /readyz to answer 200.
app-up:
	$(COMPOSE) --profile app up -d --build --wait orchestrator scheduler

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

# Drift check: lint the Conversation API spec (Step 26; hand-written, nothing is generated from
# it here), then regenerate the domain models to a temp file and diff them against the committed
# ones. Generating to stdout keeps ruff on orchestrator's config wherever the temp file lives.
check-contracts: contracts-lint
	cd orchestrator && uv run --locked openapi-spec-validator ../$(CONV_SPEC)
	@cd orchestrator && tmp=$$(mktemp) && trap 'rm -f "$$tmp"' EXIT \
		&& uv run --locked datamodel-codegen > "$$tmp" && diff -u $(MODELS) "$$tmp" \
		|| { echo "check-contracts: $(MODELS) differs from $(SPEC); run make contracts" >&2; exit 1; }

check-java:
	cd domain-services && ./mvnw -B verify

# Step 15: the Hypothesis properties and row tables of the pure transition function. Unmarked, so
# check-py runs them too; this target runs only them.
test-invariants:
	cd orchestrator && uv run --locked pytest tests/property

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
	@cd orchestrator && $(DOMAIN_ENV) uv run --locked pytest -m stack tests/stack/test_domain_contract.py

# The stack-marked tests: the anchor against MinIO's object lock and the Schemathesis contract
# tests. Needs `make up`.
check-stack: db-migrate
	@cd orchestrator && $(TEST_ENV) uv run --locked pytest -m stack

# The evaluation (Steps 17 and 23, TDD §5.2/§5.3). First every golden conversation and the red-team
# suite (content/redteam) against the running stack: the orchestrator in-process with its real
# lifespan, over the dev database (domain-services writes consent there), valkey, domain-services and
# OmniRoute in front of the stubs. Each writes a record (counts, ids, timings) and deletes its rows
# (conv, checkpoints, audit, consent, its subject key); the active prompt bundle's CONFIG_RELEASE
# stays. Then `python -m surakshasetu.eval` adds the labelled slots and intents, retrieval and the QA
# drafts (model-dependent metrics are informational against the stub), applies the gates and the
# waivers (content/eval/waivers.yaml), writes reports/eval-<ts>.json and .md, and prints the summary.
# Exit non-zero when a conversation or an enforced gate fails. Only the report step scales the TEI
# budgets (CPU retrieval); the conversations run at production budgets, as their latency is a gate.
# Needs `make up`, `make gateway-up`, `make seed-catalog` and `make kb-ingest`. Run it with nothing
# else busy on the machine.
# What the orchestrator runs with over the dev database (make serve, the goldens, the timers).
APP_ENV := SS_PG_DSN_APP="$(PG_DSN_KB)" \
	SS_PG_DSN_KEYVAULT="postgresql://keyvault_rw:$(KEYVAULT_RW_PASSWORD)@127.0.0.1:5432/surakshasetu" \
	SS_PG_DSN_ERASURE="postgresql://erasure_rw:$(ERASURE_RW_PASSWORD)@127.0.0.1:5432/surakshasetu" \
	SS_REDIS_URL="redis://127.0.0.1:6379/0" $(DOMAIN_ENV)
GOLDEN_ENV := SS_EVAL_PG_DSN_ADMIN="postgresql://postgres:$(POSTGRES_PASSWORD)@127.0.0.1:5432/surakshasetu" \
	$(APP_ENV)
EVAL_RECORDS := $(CURDIR)/orchestrator/.cache/eval-records
EVAL_REPORT = cd orchestrator && $(GOLDEN_ENV) SS_LOG_FORMAT=text $(1) \
	uv run --locked python -m surakshasetu.eval run
eval: db-migrate
	@rm -rf "$(EVAL_RECORDS)" reports/eval-latest.md && mkdir -p "$(EVAL_RECORDS)"
	@rc=0; rc2=0; \
		(cd orchestrator && $(GOLDEN_ENV) SS_EVAL_RECORDS="$(EVAL_RECORDS)" \
			uv run --locked pytest -m "golden or redteam") || rc=$$?; \
		($(call EVAL_REPORT,SS_TEI_TIMEOUT_SCALE=1000) --mode stub --records "$(EVAL_RECORDS)") \
			|| rc2=$$?; \
		cat reports/eval-latest.md 2>/dev/null; \
		if [ $$rc2 -ne 0 ]; then exit $$rc2; fi; exit $$rc

# Every §5.2 gate enforced on provisioned model routes (D2): the labelled slots and intents, retrieval
# and the QA drafts through OmniRoute's real combos. Exit 2, "eval-live requires provisioned model
# routes (D2)", while a stub still answers any route. SS_EVAL_PRIMARY_MODELS names each route's
# primary model for the fallback rate. The scripted suites stay with `make eval`.
eval-live:
	@rm -f reports/eval-latest.md; rc=0; ($(call EVAL_REPORT,) --mode live) || rc=$$?; \
		cat reports/eval-latest.md 2>/dev/null; exit $$rc

# Validate the evaluation's sets offline (TDD §7.5 step 4): the golden conversations and the
# scripted S0-S3 conversation, the retrieval golden sets, the labelled slots and intents, the
# red-team suite and the gate waivers. No stack needed.
seed-eval:
	cd orchestrator && uv run --locked pytest -q tests/unit/test_eval_sets.py \
		tests/unit/test_golden_assertions.py tests/unit/test_retrieval_metrics.py tests/unit/test_metrics.py

# The scripted S0-S3 conversation of TDD §7.4 alone (Step 21): greeting, consent, eligibility, needs,
# the recommendation with its disclosures, a chosen rider set re-quoted, the acknowledgment and the
# signed hand-off to the stub application journey. `make eval` runs it too. Same needs as eval.
# Step 24: this target keeps the session (its audit chain, consent record and subject key stay in the
# dev database) and writes its id to orchestrator/.cache/e2e-scripted-session, for `make dossier` and
# local-setup's verify-audit; `make eval` still deletes everything it creates.
E2E_SESSION := orchestrator/.cache/e2e-scripted-session
e2e-scripted: db-migrate
	@mkdir -p orchestrator/.cache && cd orchestrator && $(GOLDEN_ENV) \
		SS_GOLDEN_KEEP="$(CURDIR)/$(E2E_SESSION)" uv run --locked pytest -m golden -k scripted-s0-s3

# One look of the inactivity timers (Step 22, TDD §3.9's CC3) against the dev database: every
# post-consent session idle past its state's timeout (SS_INACTIVITY_TIMEOUTS) is paused by a timer
# turn, committed and audited like any turn; sessions before consent are never touched (V3). The
# compose `scheduler` service (profile app, `make app-up`) runs it every SS_TIMER_INTERVAL_S.
# Needs `make up` (and `make gateway-up` for the pause's output rails).
timers-once:
	@cd orchestrator && $(GOLDEN_ENV) uv run --locked python -m surakshasetu.jobs.timers --once

# Verify every audit chain active on DATE (UTC) and anchor the day's Merkle root in MinIO and
# audit.chain_anchor, as app_rw. DB=surakshasetu_test checks the test database instead.
DB ?= surakshasetu
verify-audit:
	@test -n "$(DATE)" || { echo "usage: make verify-audit DATE=YYYY-MM-DD [DB=surakshasetu]" >&2; exit 2; }
	@cd orchestrator && $(MINIO_ENV) \
		SS_PG_DSN_APP="postgresql://app_rw:$(APP_RW_PASSWORD)@127.0.0.1:5432/$(DB)" \
		uv run --locked python -m surakshasetu.audit.verify --date "$(DATE)"

# The session dossier (Step 24, TDD §4.3): one session's transcript, decisions, disclosures and
# acknowledgments, consent history and pins, verified against its chain first (a broken chain exits 1
# and writes nothing). Reads the audit and consent store as compliance_ro and payloads through the key
# service; writes reports/dossiers/dossier-<id>.{json,html} (gitignored: decrypted personal data).
# SESSION defaults to the session the last `make e2e-scripted` kept.
SESSION ?= $(shell cat $(E2E_SESSION) 2>/dev/null)
dossier:
	@test -n "$(SESSION)" || { echo "usage: make dossier SESSION=<session id> [DB=surakshasetu]" >&2; exit 2; }
	@cd orchestrator && \
		SS_PG_DSN_COMPLIANCE="postgresql://compliance_ro:$(COMPLIANCE_RO_PASSWORD)@127.0.0.1:5432/$(DB)" \
		SS_PG_DSN_KEYVAULT="postgresql://keyvault_rw:$(KEYVAULT_RW_PASSWORD)@127.0.0.1:5432/$(DB)" \
		uv run --locked python -m surakshasetu.audit.dossier --session "$(SESSION)" --out ../reports/dossiers

# TDD §7.5 step 6's DUMMY half (Step 24): the scripted S0-S3 conversation replayed with the runtime on
# env=pilot; every response carrying DUMMY text must be blocked by rail 8's RC-DUMMY, so none reaches a
# pilot customer. Pilot refuses the DUMMY bundle and privacy FAQ at load, and that refusal is unchanged:
# only the test loads them as dev (after asserting the refusal). Prints a BLOCK line per turn. Same
# needs as eval; it deletes what it creates.
verify-release-gate: db-migrate
	@cd orchestrator && $(GOLDEN_ENV) uv run --locked pytest -m golden \
		tests/golden/test_release_gate_pilot.py

# The Conversation API on 127.0.0.1:8000 over the dev database, for a demo with the terminal chat
# client (`cd orchestrator && uv run python scripts/chat.py`). Needs `make local-setup` (or `make up`,
# `make gateway-up`, `make seed-catalog` and `make kb-ingest`). Ctrl-C stops it.
serve:
	@cd orchestrator && $(APP_ENV) SS_LOG_FORMAT=text \
		uv run --locked uvicorn surakshasetu.api.app:create_app --factory --port 8000

# TDD §7.5 steps 1-6 in order, each its own target (idempotent; stops at the first failure): the
# stack and migrations, the gateway, the catalog with its hashes, the KB through the review gate,
# the evaluation sets, the invariant properties and the scripted S0-S3 conversation; then step 6
# (Step 24): every audit chain active today (UTC) verified and the day anchored, and the pilot DUMMY
# gate. In dev, anchoring today anchors an open day: the anchor row is insert-only, so a later run
# the same day logs "anchored while open" and still exits 0 (pilot and prod refuse an open day).
# Step 25: it starts the stack itself and refuses one already running (containers from an earlier
# build or session); stopped volumes are fine, so `make down && make local-setup` reruns warm.
local-setup:
	@if [ -n "$$($(COMPOSE) --profile gateway --profile app ps -q)" ]; then \
		echo "local-setup: the stack is already running; run 'make down' first" >&2; exit 1; fi
	@for t in up gateway-up seed-catalog kb-ingest kb-verify seed-eval test-invariants e2e-scripted \
		"verify-audit DATE=$$(date -u +%F)" verify-release-gate; do \
		echo "== local-setup: $$t"; $(MAKE) --no-print-directory $$t || exit 1; done

$(PLACEHOLDERS):
	@echo "$@: not implemented yet"
