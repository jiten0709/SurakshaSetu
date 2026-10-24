# Operations

How to run, configure and check the system. Everything here applies to the local Docker Compose deployment; the production deployment is upcoming (see the [roadmap](roadmap.md)).

## Prerequisites

| Need          | Detail                                                                                                                                                                                                                   |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Docker        | Compose v2. The Docker VM needs about 5 GB for the two TEI services together (the defaults OOM-kill an 8 GB VM; compose sets `--tokenization-workers=2 --max-batch-tokens=2048`). A CI full-stack eval needs about 16 GB |
| `uv`          | Python 3.12 environments for `orchestrator/` and `tools/stubs/`                                                                                                                                                          |
| JDK 21        | On `PATH` or in `JAVA_HOME` (on macOS with Homebrew: `/opt/homebrew/opt/openjdk@21`)                                                                                                                                     |
| Node 24       | `frontend/.nvmrc`; `make ui*` targets refuse another major version and print the `fnm` commands                                                                                                                          |
| Disk and time | The first TEI start downloads about 2.9 GB of pinned weights into the `tei-models` volume (30 minutes on a slow link); the first `make up` also builds MinIO from source (about 6 minutes)                               |
| amd64 hosts   | Set `TEI_IMAGE` to the `cpu-1.9.4` image (the default is `cpu-arm64`)                                                                                                                                                    |

Credentials: compose supplies dummy defaults for every secret; override them in `infra/.env` (gitignored and human-managed; never committed and never written by an agent).

## Standing up the stack

`make local-setup` (about 40 minutes the first time) performs TDD §7.5 steps 1–6, stopping at the first failure, and refuses to run if any container of profile `core`, `gateway` or `app` is running (run `make down` first):

| Stage | Target                                             | What it does                                                                                                                                                                                           |
| ----- | -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1     | `up`                                               | Starts Postgres, runs `db-migrate` (Flyway V1–V8 for `surakshasetu` and `surakshasetu_test`, then the LangGraph tables), builds images, waits for every healthcheck, creates the object-locked buckets |
| 1     | `gateway-up`                                       | Starts OmniRoute and applies and verifies `seed.json`                                                                                                                                                  |
| 2     | `seed-catalog`                                     | Loads fictitious products, riders, disclosures, reference data and notices; computes and stores hashes; a second run changes 0 rows                                                                    |
| 3     | `kb-ingest`, `kb-verify`                           | Ingests the DUMMY corpora through the review gate into three immutable snapshots and checks the result against the golden sets                                                                         |
| 4     | `seed-eval`                                        | Offline validation of every labelled set, golden conversation, retrieval set, red-team conversation and waiver                                                                                         |
| 5     | `test-invariants`, `e2e-scripted`                  | The FSM property tests; the scripted S0 → S3 conversation ending in HANDOFF (the stub journey logs `verified intake`)                                                                                  |
| 6     | `verify-audit DATE=<today>`, `verify-release-gate` | Verifies and anchors the audit chains; replays the conversation in pilot mode and requires every DUMMY release to be blocked                                                                           |

`make start` is the everyday command: it runs `up`, then each of `gateway-up` and `seed-catalog` + `kb-ingest` only when its probe (`gateway-verify`, `kb-verify`) fails, then `app-up`. After an ingest it runs `kb-verify` again and stops on a problem. It runs no tests; `local-setup` is the proof. If only the `qdrant` volume was removed while `pgdata` keeps the snapshot rows, ingest is a no-op and that check fails: remove both (see [Cold start](#cold-start)).

To chat with it, follow the [README quick start](../README.md#quick-start).

### The API in compose

`make start` (or `make app-up` after `make local-setup`, or `make up` and `make gateway-up`) runs the same API from the orchestrator image (profile `app`) together with the `scheduler`, which pauses post-consent sessions idle past their state's timeout (`SS_TIMER_INTERVAL_S`, default 60 s), and the chat UI's dev server (`ui`, 127.0.0.1:5173; see [Using the UI](#using-the-ui)).

- The API publishes the same port as `make serve` (127.0.0.1:8000), and `ui` the same as `make ui` (127.0.0.1:5173), so run one or the other.
- Both services take `make serve`'s settings with service hostnames, overridable from `infra/.env`.
- To see them: `docker compose -f infra/compose.yaml --profile core --profile app ps` (the `app` services depend on `core`, so name both profiles).

### Cold start

`make local-setup` refuses a running stack, so `make down` comes first; on stopped volumes it reruns safely. To start from empty data, remove the volumes after `make down` (this deletes every local session, audit record and anchor):

```sh
make down
docker volume rm surakshasetu_pgdata surakshasetu_qdrant surakshasetu_qdrant-snapshots \
  surakshasetu_minio surakshasetu_omniroute-data   # keep surakshasetu_tei-models: 2.9 GB of weights
make local-setup   # or `make start` to skip the proof and run the app
```

A cold start was proved in 3 min 05 s on warm model weights.

### Demo notes

- The models are stubs, so their wording is canned and free text is understood only by the deterministic parsers. Use the quick replies and short answers ("34", "411001", "no", "12 lakh"); the seed pincodes are in `content/seed/catalog/reference.yaml`.
- Retrieval recall and precision for the regulatory and tax collections show as WAIVED in the evaluation report (see the [roadmap](roadmap.md)).

## Using the UI

The chat UI (`frontend/`) is a thin client of the Conversation API: it shows what the server releases and sends what the customer types or picks, nothing more.

1. **Start everything:** `make start`: the API on 127.0.0.1:8000, the timers and the UI on 127.0.0.1:5173. Open http://127.0.0.1:5173. The dev server proxies `/v1` to the API, so the browser talks to one origin.
   - The UI runs in the compose service `ui` (`frontend/Dockerfile`: Node and nothing else). `frontend/` is bind-mounted, so an edit reloads the page; its `node_modules` is the container's own, in the volume `ui-node-modules` (the host's is hidden). The first start runs `npm ci` there (about a minute); a later start does it only when `package-lock.json` changed.
   - Nothing else is needed on the host: no Node, no second terminal.
2. **Or on the host:** `make serve` for the API and `make ui` for the UI, each in its own terminal, with the `app` services stopped (they share ports 8000 and 5173).
   - The UI targets need Node 24 (`frontend/.nvmrc`); with [fnm](https://github.com/Schniz/fnm): `fnm install 24 && fnm use 24`. Each target refuses another major and runs `npm ci` when the lock file changed.
   - Answer the questions in short words or with the buttons ("34", "411001", "no", "24 lakh"). A reload starts a new conversation: the session token is held in memory only.
3. **The dev inspector (optional):** a panel under the input with the conversation's state, the transition row and its reason code per turn, read from the audit trail's headers (never its payloads).
   - Copy `frontend/.env.example` to `frontend/.env.local` (gitignored) and set `SS_COMPLIANCE_API_KEY` to the API's compliance key (the dev default in `orchestrator/src/surakshasetu/config.py`, unless your `orchestrator/.env` sets another).
   - Set `VITE_INSPECTOR=1` in `.env.local` (or run `VITE_INSPECTOR=1 make ui` on the host). The `ui` container reads the same file through the bind mount and keeps no key in compose or in the image; Vite reads it at start, so run `docker compose -f infra/compose.yaml --profile core --profile app restart ui` after editing it.
   - Only the dev server's proxy reads the key; it never reaches the browser. Without it the panel says "not configured" and the chat works as usual.
   - It exists only in the dev server: `npm run build` contains none of it (a test builds the bundle and checks).
4. **The browser smoke test:** `cd frontend && npx playwright install chromium` once, then `make ui-e2e` with `make serve` running.
   - It drives the scripted customer (TDD §7.4) through Chromium from the greeting to the signed hand-off, about 2 minutes (turns are paced for the API's rate limit), and checks the recommendation at phone width.
   - A dev server already on :5173 (the `ui` container or `make ui`) is reused; otherwise it starts one itself. With the API down it stops at once and names `make serve`.
   - A failure names the step only, never message text. It is not part of `make check`.
5. **Checks:** `make ui-check` (the types against the spec, tsc, ESLint, the unit tests and the production build); `make ui-types` regenerates the types after a spec change.

## Make target reference

The root `Makefile` is the single entry point; CI runs the same targets. Targets that touch the stack read the role passwords and tokens from `infra/.env` (else the dummy defaults), so run the `db`, `stack` and `golden` suites through make.

| Group      | Target                                      | What it does                                                                                                              |
| ---------- | ------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- |
| Stack      | `up` / `down` / `logs`                      | Start the core profile (Postgres first, then the migrations, then everything else) / stop every profile / follow the logs |
|            | `gateway-up` / `gateway-verify`             | Start OmniRoute and apply `infra/omniroute/seed.json` / read every seeded value back (exit 1 on drift)                    |
|            | `app-up`                                    | The orchestrator, the scheduler and the chat UI's dev server in compose (profile `app`)                                   |
|            | `serve`                                     | The Conversation API on the host, 127.0.0.1:8000                                                                          |
|            | `local-setup`                               | TDD §7.5 steps 1–6, in order, stopping at the first failure                                                               |
|            | `start`                                     | `up`, then the gateway, catalog and knowledge base only where missing, then `app-up`: one command to the running demo     |
| Data       | `db-migrate`                                | Flyway on both databases, then the checkpointer's tables                                                                  |
|            | `seed-catalog`                              | Catalog, disclosure registry, notices and reference masters from `content/seed`                                           |
|            | `kb-ingest` / `kb-verify` / `kb-chunks`     | The knowledge base through the review gate into Qdrant / check it / list the approved chunk ids                           |
|            | `contracts` / `contracts-lint`              | Regenerate the Python models from the contract / validate the contract                                                    |
| Checks     | `check`                                     | `check-py` (contract drift, ruff, mypy, unit tests), `check-java`, `check-stubs`                                          |
|            | `check-db` / `check-stack` / `check-ingest` | The `db`-marked tests / the `stack`-marked tests / the ingestion tests (need the stack)                                   |
|            | `contract-test`                             | Schemathesis against the running domain tier                                                                              |
|            | `test-invariants`                           | The transition function's property tests                                                                                  |
| Evaluation | `eval`                                      | Every golden and red-team conversation, then the TDD §5.2/§5.3 gates (stub models)                                        |
|            | `eval-live`                                 | Every §5.2 gate on provisioned model routes (exit 2 until D2)                                                             |
|            | `seed-eval`                                 | Validate the evaluation sets offline                                                                                      |
|            | `e2e-scripted`                              | The scripted S0–S3 conversation alone; keeps its session for `dossier`                                                    |
|            | `verify-release-gate`                       | The scripted conversation on pilot settings: every DUMMY release must be blocked                                          |
|            | `calibrate-retrieval` / `eval-retrieval`    | Retrieval thresholds / retrieval metrics against the golden sets                                                          |
|            | `bakeoff-embed` / `bakeoff-rerank`          | The Step 12 model bake-off (profile `bakeoff`)                                                                            |
| Audit      | `verify-audit DATE=YYYY-MM-DD`              | Verify every chain active that day and anchor its Merkle root                                                             |
|            | `dossier [SESSION=<id>]`                    | One session's verified compliance dossier (JSON and HTML)                                                                 |
| Jobs       | `timers-once`                               | One look of the inactivity timers on the host                                                                             |
| UI         | `ui`                                        | The chat UI's dev server on the host, 127.0.0.1:5173 (`app-up` runs it in compose; `VITE_INSPECTOR=1` for the inspector)  |
|            | `ui-types` / `ui-check`                     | Regenerate the TypeScript types from the Conversation API spec / drift check, typecheck, lint, test and build             |
|            | `ui-e2e`                                    | The browser smoke test (Playwright): the scripted customer to the hand-off, against `make serve`                          |

`check-py` runs `check-contracts`, `ruff check`, `ruff format --check`, `mypy` strict on `src/surakshasetu`, then pytest without the `db`/`stack`/`golden`/`redteam`/`live` markers. `check-java` runs `./mvnw -B verify` (surefire, failsafe `*IT` with Testcontainers, and the spotless check). Marked suites need passwords from `infra/.env` and so run only through `make`.

## Configuration

`Settings` (`config.py`) reads `SS_`-prefixed environment variables and `.env`. In `pilot` and `prod` it fails fast, with one line per problem and no values echoed, unless all of the following are **explicitly** set (a default does not count): `pg_dsn_app`, `redis_url`, `domain_token`, `domain_internal_token`, `ops_api_key`, `compliance_api_key`, `advisor_api_key`, `pg_dsn_erasure`, `pg_dsn_compliance`, `kek_b64`, `pg_dsn_keyvault`, `minio_endpoint`, `minio_access_key`, `minio_secret_key`, `tsa_key_path`, `anchor_retention_days`, `gateway_base_url`, `gateway_api_key`, `tei_embed_url`, `tei_rerank_url`, `qdrant_url`, `intake_signing_key_b64`, `journey_url`. Additionally `tei_timeout_scale` must be exactly 1 and every inactivity timeout at least 60 s.

| Theme                          | Settings (default)                                                                                                                                                                                                                                                                                                    |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Environment, logging           | `env` (`dev`), `log_level` (`INFO`), `log_dir` (none), `log_format` (`json`)                                                                                                                                                                                                                                          |
| Runtime                        | `prompt_bundle` (`pb-2026.10.8`), `session_ttl_days` (30), `session_lock_ttl_s` (30), `idempotency_ttl_s` (86400), `rate_limits` (`{60: 20, 3600: 200}`), `side_query_max_stack` (2), `injection_hit_limit` (3), `pg_pool_max` (20)                                                                                   |
| Thresholds (never prompt text) | `confidence_accept` (0.8), `confidence_readback_floor` (0.6), `injection_score_threshold` (0.5), `normalise_token_cap` (500), `profile_sufficiency_min` (0.7), `rediscovery_loop_limit` (2), `low_confidence_streak_limit` (2), `invalid_input_limit` (3), `side_query_offer_after` (5), `objection_repeat_limit` (2) |
| Rails                          | `output_lexicon` (`2026.10.1`), `verify_sample_rate` (0.1)                                                                                                                                                                                                                                                            |
| Model path                     | `gateway_base_url` (`http://127.0.0.1:20130/v1`), `tei_embed_url`, `tei_rerank_url`, `embed_dim` (1024), `embed_query_prefix`, `tei_timeout_scale` (1.0)                                                                                                                                                              |
| Timers and hand-off            | `inactivity_timeouts` (600 s per state), `timer_interval_s` (60), `advisor_queue_open` (true), `journey_url`                                                                                                                                                                                                          |
| Keys and retention             | `kek_b64`, `key_retention_days` (397), `anchor_retention_days` (1 in dev)                                                                                                                                                                                                                                             |
| Evaluation                     | `eval_primary_models` (empty)                                                                                                                                                                                                                                                                                         |

The thresholds are deployment configuration and **not** session pins.

## Health and readiness

`GET /healthz` is liveness only. `GET /readyz` checks, in this order: `database` (reading the active kill switches), `redis`, `domain` (`get_versions`, 150 ms), `omniroute` (`GET /healthz` on the gateway), `bundle` (exactly what a turn's `load` node calls: not found, hash mismatch, DUMMY in pilot or prod, kill-switched), and in pilot or prod `dummy_gate` (the RC-DUMMY rail must block, and the privacy FAQ must load in both locales). Dependencies run concurrently with a 2 s timeout each. The answer is 200 `{status: ready, version}` or 503 problem+json with `failing: [names]`, never a value, URL or error text. After start, readiness flips on dependency health and on a kill switch against the active bundle; `Runtime.open` still refuses to start on a bad or DUMMY (pilot) bundle.

## Logging policy

Logs are diagnostics, **never the audit record**: anything an auditor needs goes through `audit/`.

- Entry points configure logging once (`surakshasetu.logging.configure_logging`); stdout is always JSON lines; `SS_LOG_DIR` adds one rotating file per top-level package. Fixed fields only: `ts, level, logger, msg, request_id, session_id, exc`.
- Levels: INFO for stage boundaries and outcomes, DEBUG for counts and timings, WARNING for degraded-but-continuing, ERROR for `logger.exception`, CRITICAL for pages only (`AUDIT_CHAIN_BROKEN`).
- **Never logged:** utterances, slot values, request or response bodies (including problem `detail`), URL paths or query strings, customer-identifying ids, pincodes, `str(exc)` of an HTTP error, DSNs, tokens, keys, ciphertext. After Step 10 only the `redacted` form of customer text may appear. `httpx`, `httpcore`, `presidio-*` and `uvicorn.access` are pinned to WARNING because their INFO or DEBUG lines carry URLs, matched words or session ids.
- Every new code path that handles customer data needs a unit test proving a sentinel value does not reach log output.
- Java logs are ECS JSON via SLF4J with the same never-log list.

## Security and privacy posture (as built)

| Area           | Control                                                                                                                                                       |
| -------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Personal data  | Encrypted under a per-subject key in the conversation and audit tables; redacted in logs and for any non-self-hosted route; erasure destroys the key           |
| Authentication | Anonymous session bearer token (stored as SHA-256); service token between tiers (mTLS is a gate); per-role bearer keys for internal endpoints (SSO is a gate) |
| Authorisation  | Postgres roles with least privilege; kill switch needs a second, internal token; the domain tier answers 401/403 before reading a body                        |
| Gateway        | Loopback only; API key required; policy-restricted app key; no logging of prompts; compression, memory, cache and auto-routing off                            |
| Injection      | Normalisation, heuristics, classifier, escaped tagged inputs, grounding and lexicon rails, a compliance-owned pack                                            |
| Supply chain   | Image tags and digests pinned; MinIO built from pinned commits; npm `ignore-scripts` and exact versions; `uv.lock` committed                                  |
| Secrets in Git | None. Compose defaults are dummy values; `infra/.env`, `frontend/.env.local` and dossiers are gitignored                                                      |

## Running one test

```sh
cd orchestrator && uv run pytest tests/unit/test_readyz.py::test_ready_when_every_check_holds   # conversation tier
cd domain-services && ./mvnw -Dtest=DisclosureRegistryTest test    # domain tier, unit (surefire)
cd domain-services && ./mvnw -Dit.test=HealthIT verify              # domain tier, *IT (failsafe, Testcontainers)
cd tools/stubs && uv run pytest tests/test_journey.py               # stubs
```

- **Marked suites:** they need the stack and the passwords from `infra/.env`, so run them through make. `make check-db` (`db`), `make check-stack` (`stack`), `make e2e-scripted` (one golden conversation), `make eval` (`golden` and `redteam`).
