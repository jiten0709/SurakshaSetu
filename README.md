# SurakshaSetu

An AI-assisted conversational advisor for life and term insurance in India. It guides a customer from consent, through eligibility and needs discovery, to a compliant, explainable plan recommendation.

> **Phase 1, Steps 1–25 of 33 done: the backend is demo-ready.**
>
> - **What works:** the whole S0–S3 journey, end to end. That covers consent, eligibility, the Quote-Only path, needs discovery, a ranked and cited recommendation with hash-verified disclosures, and a signed hand-off to the application journey. The cross-cutting handlers (FAQ, objections, human escalation, pause, data erasure) work too.
> - **Evidence:** `make eval` passes 208 of 208 (207 golden and red-team conversations plus the pilot DUMMY gate), and 0 of 157 attacks succeed. Compliance can render a verified dossier for any session.
> - **How it runs:** the orchestrator runs on the host or in compose, with readiness checks and an inactivity scheduler.
> - **Not real yet:** every product, rule, rate and document is DUMMY data, and the language models are local stubs until the hosting decision (D2).
> - **Next:** Steps 26–30 build the chat UI. Steps 31–33 (WORM audit streaming, observability, retention) are the production layer; see [Release gates](#release-gates-before-any-pilot-traffic).

## Core idea

- **The LLM handles language only.** It interprets what the customer says and explains results.
- **Every decision is deterministic.** Eligibility, suitability, cover sizing, premiums, ranking and state transitions come from versioned rule services, so an auditor can reproduce any recommendation from its inputs.
- **Legally significant text comes from templates.** Consent notices, disclosures and hand-off scripts are never generated, and every mandatory disclosure is recorded in a hash-chained audit ledger.
- **Consent comes first.** No data is stored and no service is called before the customer consents, and a withdrawal takes effect in the same turn.

## Phase 1 scope

- **In scope:**
  - S0–S3 and the Quote-Only path.
  - The FAQ engine, the objection handler, human escalation, pause, exit, exit (advisory) and data erasure.
  - The launch product set: term plans with riders.
- **The hand-off:** S3 ends by posting a signed intake payload to the existing application journey. The payload carries the disclosures' hashes, not copies. S4–S8 are later phases.
- **The data:** every product, rule, rate, disclosure and knowledge-base document is fictitious DUMMY data. Pilot and production refuse DUMMY content (see [Release gates](#release-gates-before-any-pilot-traffic)).

## Architecture

- **Conversation tier (`orchestrator/`):** Python 3.12, FastAPI and LangGraph, with a PostgreSQL checkpointer.
  - Each turn runs input rails and turn analysis, then the pure transition function, the domain calls, retrieval, composition (a template, or cited generation) and the output rails.
  - The audit record commits before the response is released.
- **Domain tier (`domain-services/`):** Java 21, Spring Boot and DMN tables. It is the only place that decides consent, eligibility, suitability, quotes and ranking, and it owns the product catalog and the disclosure registry. The orchestrator calls it over an OpenAPI contract (`contracts/`). The model never calls it.
- **Model path:**
  - Chat routes (guard, NLU, generation, claim verification) go through SurakshaSetu's own hardened OmniRoute gateway. Code names a route and a data class, never a model.
  - Embeddings and reranking go straight to self-hosted TEI.
- **Retrieval:** three Qdrant collections (regulatory, product, tax) with hybrid dense and BM25 search, a reranker, and a sufficiency gate that abstains on weak evidence. Numbers and mandatory text never come from retrieval. They come only from the catalog and the registry.
- **Storage:** PostgreSQL holds the schemas `conv`, `consent`, `catalog`, `audit`, `keyvault` and `langgraph`.
  - Personal data is encrypted under a per-subject key, so erasure is crypto-shredding.
  - The audit trail is a hash chain, anchored daily in object-locked MinIO.
  - Valkey holds session locks, idempotency hints and rate limits.

## Conversation journey

| State | Stage                                            | Phase |
| ----- | ------------------------------------------------ | ----- |
| S0    | Greeting, AI disclosure and consent              | 1     |
| S1    | Rapport and eligibility screening                | 1     |
| S2    | Needs discovery and suitability assessment       | 1     |
| S3    | Plan recommendation, comparison and disclosures  | 1     |
| S4    | Application intake and health declaration        | 2     |
| S5    | Identity verification and KYC                    | 2     |
| S6    | Payment authorisation and mandate                | 2     |
| S7    | Underwriting liaison                             | 3     |
| S8    | Post-sale onboarding                             | 3     |

These layers work across every state: FAQ engine, objection handler, human escalation, pause/resume, and data erasure.

![Conversation state flow](assets/images/state-flow.png)

_State flow from the product spec. Phase 1 builds only S0–S3 (green) and hands off to the existing application journey instead of S4._

## Repository layout

| Path               | What it holds                                                                          |
| ------------------ | -------------------------------------------------------------------------------------- |
| `orchestrator/`    | Conversation tier: Python 3.12, FastAPI and LangGraph                                  |
| `domain-services/` | Domain tier: Java 21, Spring Boot and DMN rules (eligibility, suitability, consent, catalog) |
| `tools/stubs/`     | Local stand-ins for the model and external systems                                     |
| `infra/`           | Docker Compose stack (Postgres, Valkey, Qdrant, MinIO, TEI, OmniRoute, the orchestrator image) and Flyway migrations |
| `contracts/`       | The OpenAPI contract between the two tiers                                             |
| `content/`         | Seed data, prompt bundles, lexicons, the privacy FAQ, golden sets and the red-team suite |

## Quick start

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, and JDK 21.

```sh
make local-setup  # TDD §7.5 steps 1-6: stack, gateway, catalog, knowledge base, eval sets, invariant
                  # tests, the scripted S0-S3 conversation, audit verification and the pilot DUMMY gate
                  # (first run: ~40 min of downloads). It refuses a running stack: `make down` first.
make serve        # the Conversation API on 127.0.0.1:8000 (leave it running)
```

Then chat with it in a second terminal:

```sh
cd orchestrator && uv run python scripts/chat.py            # --locale hi-IN for Hindi
```

Type a number to pick a quick reply, `f` to fill the consent form, `/erase` to delete your data, `/quit` to leave.

Other targets:

```sh
make check        # lint, type-check and test all tiers
make eval         # every golden and red-team conversation, then the TDD §5.2/§5.3 gates (~13 min)
make e2e-scripted # the scripted S0-S3 conversation alone, ending in the signed hand-off
make down         # stop the stack
```

### Demo notes

- The models are stubs, so their wording is canned and free text is understood only by the deterministic parsers. Use the quick replies and short answers ("34", "411001", "no", "12 lakh"); the seed pincodes are in `content/seed/catalog/reference.yaml`.
- Retrieval recall and precision for the regulatory and tax collections show as WAIVED in the eval report: they miss their targets on the DUMMY corpus until the reranker decision.

### The API in compose

`make app-up` (after `make local-setup`, or `make up` and `make gateway-up`) runs the same API from the orchestrator image (profile `app`). It also starts the `scheduler`, which pauses post-consent sessions idle past their state's timeout (`SS_TIMER_INTERVAL_S`, default 60 s).

- The API publishes the same port as `make serve` (127.0.0.1:8000), so run one or the other.
- Both services take `make serve`'s settings with service hostnames, overridable from `infra/.env`.
- To see them: `docker compose -f infra/compose.yaml --profile core --profile app ps` (the `app` services depend on `core`, so name both profiles).

### Health and readiness

| Endpoint | Answers | Checks |
| --- | --- | --- |
| `GET /healthz` | 200 while the process serves (the image's HEALTHCHECK) | nothing: liveness only |
| `GET /readyz` | 200 `{"status": "ready"}`, or 503 problem+json with `failing: [...]` | `database`, `redis`, `domain` (`/v1/meta/versions`), `omniroute` (its `/healthz`), `bundle` (the pinned prompt bundle loads, and isn't kill-switched or DUMMY in pilot/prod), `dummy_gate` (in pilot/prod, rail 8 blocks DUMMY text and the privacy FAQ loads) |

The 503 names the failing checks only, never a value, URL or error text.

### Cold start

`make local-setup` refuses a running stack, so `make down` comes first; on stopped volumes it reruns safely. To start from empty data, remove the volumes after `make down` (this deletes every local session, audit record and anchor):

```sh
make down
docker volume rm surakshasetu_pgdata surakshasetu_qdrant surakshasetu_qdrant-snapshots \
  surakshasetu_minio surakshasetu_omniroute-data   # keep surakshasetu_tei-models: 2.9 GB of weights
make local-setup
```

## Make targets

The root `Makefile` is the single entry point; CI runs the same targets. Targets that touch the stack read the role passwords and tokens from `infra/.env` (else the dummy defaults), so run the `db`, `stack` and `golden` suites through make.

| Group | Target | What it does |
| --- | --- | --- |
| Stack | `up` / `down` / `logs` | Start the core profile (Postgres first, then the migrations, then everything else) / stop every profile / follow the logs |
| | `gateway-up` / `gateway-verify` | Start OmniRoute and apply `infra/omniroute/seed.json` / read every seeded value back (exit 1 on drift) |
| | `app-up` | The orchestrator and the scheduler in compose (profile `app`) |
| | `serve` | The Conversation API on the host, 127.0.0.1:8000 |
| | `local-setup` | TDD §7.5 steps 1–6, in order, stopping at the first failure |
| Data | `db-migrate` | Flyway on both databases, then the checkpointer's tables |
| | `seed-catalog` | Catalog, disclosure registry, notices and reference masters from `content/seed` |
| | `kb-ingest` / `kb-verify` / `kb-chunks` | The knowledge base through the review gate into Qdrant / check it / list the approved chunk ids |
| | `contracts` / `contracts-lint` | Regenerate the Python models from the contract / validate the contract |
| Checks | `check` | `check-py` (contract drift, ruff, mypy, unit tests), `check-java`, `check-stubs` |
| | `check-db` / `check-stack` / `check-ingest` | The `db`-marked tests / the `stack`-marked tests / the ingestion tests (need the stack) |
| | `contract-test` | Schemathesis against the running domain tier |
| | `test-invariants` | The transition function's property tests |
| Evaluation | `eval` | Every golden and red-team conversation, then the TDD §5.2/§5.3 gates (stub models) |
| | `eval-live` | Every §5.2 gate on provisioned model routes (exit 2 until D2) |
| | `seed-eval` | Validate the evaluation sets offline |
| | `e2e-scripted` | The scripted S0–S3 conversation alone; keeps its session for `dossier` |
| | `verify-release-gate` | The scripted conversation on pilot settings: every DUMMY release must be blocked |
| | `calibrate-retrieval` / `eval-retrieval` | Retrieval thresholds / retrieval metrics against the golden sets |
| | `bakeoff-embed` / `bakeoff-rerank` | The Step 12 model bake-off (profile `bakeoff`) |
| Audit | `verify-audit DATE=YYYY-MM-DD` | Verify every chain active that day and anchor its Merkle root |
| | `dossier [SESSION=<id>]` | One session's verified compliance dossier (JSON and HTML) |
| Jobs | `timers-once` | One look of the inactivity timers on the host |

## Compose profiles

| Profile | Started by | Services (all bound to 127.0.0.1) |
| --- | --- | --- |
| `core` | `make up` | postgres :5432, valkey :6379, qdrant :6333/:6334, minio :9000, domain-services :8080, tei-embed :8081, tei-rerank :8082, stubs :8090, and the one-shots flyway and minio-init |
| `gateway` | `make gateway-up` | omniroute :20130 (SurakshaSetu's own, hardened; see `infra/omniroute/HARDENING.md`) |
| `app` | `make app-up` | orchestrator :8000, scheduler |
| `bakeoff` | `make bakeoff-embed` / `bakeoff-rerank` | the challenger TEI models :8083 and :8084 |

## Running one test

```sh
cd orchestrator && uv run pytest tests/unit/test_readyz.py::test_ready_when_every_check_holds   # conversation tier
cd domain-services && ./mvnw -Dtest=DisclosureRegistryTest test    # domain tier, unit (surefire)
cd domain-services && ./mvnw -Dit.test=HealthIT verify              # domain tier, *IT (failsafe, Testcontainers)
cd tools/stubs && uv run pytest tests/test_journey.py               # stubs
```

- **Java:** Maven needs JDK 21 on `PATH` or in `JAVA_HOME` (on a Mac with Homebrew: `JAVA_HOME=/opt/homebrew/opt/openjdk@21`).
- **Marked suites:** they need the stack and the passwords from `infra/.env`, so run them through make. `make check-db` (`db`), `make check-stack` (`stack`), `make e2e-scripted` (one golden conversation), `make eval` (`golden` and `redteam`).

## Release gates before any pilot traffic

The demo runs on DUMMY data, stub models and local stand-ins. **No pilot customer may reach it until every item below is closed.** Steps 31–33 (WORM audit streaming, observability, retention) are further prerequisites for production, not for the demo.

1. **Open decisions (TDD §6.2).**
   - D1: the state specification's missing parts, and confirmation of deviations V1–V7.
   - D2: frontier model hosting.
   - D3: the AI gateway.
   - D6: the suitability thresholds.
   - D7: the retention schedule and session TTL.
   - D8: the advisor review after the pilot.
   - D4 (LangGraph) and D5 (term with riders) are decided.
2. **Compliance replaces every DUMMY record:** the catalog, the disclosure sets, the consent notices, the prompt-bundle templates, the privacy FAQ, the knowledge-base corpora and the lexicons. Every hi-IN text needs an approved translation.
   - Pilot and production refuse a DUMMY bundle or FAQ at load, and rail 8 blocks any DUMMY text.
   - `make verify-release-gate` proves that on the seed.
3. **Actuarial and compliance replace the DUMMY rate table, ranking weights, rider rules, actuarial params and DMN values (D6).** They ship as new versions; shipped versions are never edited in place.
4. **The gateway (D3):** an InfoSec review of the hardened OmniRoute (the deployment gates in `infra/omniroute/HARDENING.md`), or the decision to move to LiteLLM.
5. **Model routes (D2):** routes provisioned, and `make eval-live` meeting every §5.2 gate. It exits 2 while a stub answers any route.
6. **Retrieval** (an addition to the guide's list): the reranker decision (Step 12), and the retrieval waivers in `content/eval/waivers.yaml` removed once the recall and precision gates are met on the real corpus.
7. **Keys:** an India-region KMS/HSM behind the key service, in place of the local KEK.
8. **Timestamps:** a real RFC 3161 TSA for the daily audit anchor, in place of the stub signature.
9. **mTLS between the tiers:** the orchestrator, domain-services and the gateway.
10. **Staff SSO** for the internal endpoints (ops, compliance and advisor keys today) and the gateway's admin surface.
11. **Legal sign-off** on the retention schedule (D7) and on the GST treatment (the DUMMY rate table applies 0% until tax confirms).
12. **Least-privilege database roles in the deploy environment:** the V7 role matrix, and a PUT-only MinIO user for the anchors.
13. **The canary pipeline:** two-person sign-off and a 5% canary for every governed artefact (`RELEASING.md`, `CODEOWNERS`).
14. **An advisor review tool for the pilot** (D8). Today the hand-off queue is an internal API only.

Compliance can inspect any session with `make dossier SESSION=<id>`: transcript, decisions, disclosures, acknowledgments and consent history, verified against the audit chain.

## Note

All reference data is fictitious, including product UINs, disclosure sets and knowledge-base content.
