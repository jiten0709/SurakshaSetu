# Roadmap and status

**Phase 1 is complete.** Its engineering scope (Steps 1–30: States S0–S3, Quote-Only and the support states, the signed hand-off, the evaluation and release gates, and the chat UI) is built and passes its gates on stub models and DUMMY content. Everything after it is **upcoming**: the production layer (Steps 31–33), the pre-pilot gates and the open decisions.

**Carry-overs from Phase 1**, kept visible:

- The retrieval recall and precision gates for the regulatory and tax collections are **not met**. Compliance waived them in `content/eval/waivers.yaml`, and every run shows them as WAIVED. The reranker decision is open.
- `make eval-live`, which enforces every quality gate on real model routes, exits 2 until decision D2 provisions them. The model-dependent metrics are therefore unjudged.
- Every product, rate, rule threshold, disclosure and consent text is DUMMY.

## Project status and timeline

Steps 1–30 are Phase 1. Steps 31–33 are the production layer and are upcoming.

| Step | Title                                                                | Status                                                                                            |
| ---- | -------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 1    | Monorepo scaffold, core infra, CI                                    | Done                                                                                              |
| 2    | Postgres schemas, roles, grants (Flyway V1–V7)                       | Done                                                                                              |
| 3    | OpenAPI tier contract, code generation, JCS test vectors             | Done                                                                                              |
| 4    | Subject keys, audit hash chain, WORM anchoring                       | Done                                                                                              |
| 5    | Consent, catalogue, disclosure registry, reference data, seed loader (+ migration V8) | Done                                                                                              |
| 6    | Eligibility and suitability DMN, cover formula                       | Done                                                                                              |
| 7    | Ranker, quote adapter, contract tests                                | Done                                                                                              |
| 8    | Stub model, gateway adapter, TEI                                     | Done                                                                                              |
| 9    | Hardened OmniRoute gateway                                           | Done                                                                                              |
| 10   | Input rails and turn analysis                                        | Done                                                                                              |
| 11   | KB ingestion, seed corpora, golden sets                              | Done                                                                                              |
| 12   | Retrieval service                                                    | Done, with a carry-over: the regulatory and tax recall/precision gates are not met (waived; reranker decision open) |
| 13   | Prompt bundle, envelope, placeholders, citations, composer           | Done                                                                                              |
| 14   | Output rails and release policy                                      | Done                                                                                              |
| 15   | Pure transition function and invariant property tests                | Done                                                                                              |
| 16   | Runtime: store, API, LangGraph, audit-before-release                 | Done                                                                                              |
| 17   | Cross-cutting handlers and the golden harness                        | Done                                                                                              |
| 18   | State 0                                                              | Done                                                                                              |
| 19   | State 1 and Quote-Only                                               | Done                                                                                              |
| 20   | State 2                                                              | Done                                                                                              |
| 21   | State 3 and the signed hand-off                                      | Done                                                                                              |
| 22   | Side-queries, objections, timers, the §3.9 edge-case matrix          | Done                                                                                              |
| 23   | Evaluation harness, red-team suite, release gates                    | Done                                                                                              |
| 24   | Session dossier, pilot DUMMY gate, `make local-setup`                | Done                                                                                              |
| 25   | Orchestrator image, `/readyz`, timer scheduler, runbook              | Done                                                                                              |
| 26   | Conversation API contract and drift check                            | Done                                                                                              |
| 27   | Frontend scaffold, typed client, chat core                           | Done                                                                                              |
| 28   | S0 consent form, language switch, erase                              | Done                                                                                              |
| 29   | S3 view: cards, disclosures, acknowledgments, sources, hand-off      | Done                                                                                              |
| 30   | Dev inspector, Playwright smoke test                                 | Done                                                                                              |
| 31   | Audit streaming to WORM                                              | **Upcoming**                                                                                      |
| 32   | Observability: spans, Langfuse, KPI fields                           | **Upcoming**                                                                                      |
| 33   | Retention, crypto-shredding job, production readiness                | **Upcoming**                                                                                      |

## The production layer: Steps 31–33

These start only when the project owner says so. They are prerequisites for production traffic, not for the demo.

| Step                            | Scope                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    | Notes                                                                                                                                                                                                                                   |
| ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **31. Audit streaming to WORM** | Kafka (KRaft) and Debezium in a new compose profile `events`, with a `pgoutput` publication on `audit.audit_event` only and topic `audit.events`. A new Flyway migration creates the `debezium` and `audit_maintenance` roles. A `worm-archiver` job writes each event exactly as stored to MinIO `audit-archive/<session_id>/<seq>.json` with COMPLIANCE object lock (`SS_ARCHIVE_RETENTION_DAYS`), idempotently, exporting consumer lag. `make verify-audit` is extended to reconcile the hot store against the archive | `wal_level=logical` is already set on the local Postgres, but no `events` profile exists yet. There is no outbox table: `audit.audit_event` is itself the change-data-capture source |
| **32. Observability**           | OpenTelemetry for FastAPI, httpx and psycopg to an OTLP collector (profile `obs`). Span attributes: route, `fsm_state`, `turn_seq`, `rail_rule_id`, `served_model`; never paths with ids, text, slots or tokens. Self-hosted Langfuse with `MODEL_CALL` traces (redacted envelopes only, 90-day TTL). Document the event fields behind the TDD §5.1 and §5.3 KPIs. Test: a Presidio scan with sentinels proves no PII in spans                                                                                            | ClickHouse and Grafana dashboards are pilot work                                                                                                                                                                                        |
| **33. Retention**               | `jobs/retention.py` and `make retention-once`: sessions past the 30-day TTL are hard-deleted through `erase_session`; keys past `destroy_after` go through `KeyService.destroy_due`. A documented procedure for the hot store beyond 13 months through `audit_maintenance` (production drops monthly partitions). `/readyz` extended with Kafka, Debezium `RUNNING`, MinIO object lock and Langfuse. A production-infrastructure README section. Test: chains still verify after shredding                                | `erase_session` is already the function this job will reuse                                                                                                                                                                             |

## Release gates before any pilot traffic

`README.md` lists 14 items, all open; no pilot customer may reach the system until each is closed.

| #   | Gate                                                                                                                                                                               | Owner                 |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------- |
| 1   | Open decisions D1, D2, D3 (InfoSec review), D6, D7, D8 (see [Open decisions](#open-decisions))                                                                                                              | various               |
| 2   | Replace every `DUMMY` record: catalogue, disclosure sets, consent notices, bundle templates, privacy FAQ, KB corpora, lexicons; approved Hindi translations for every `hi-IN` text | compliance, legal     |
| 3   | Replace the DUMMY rate table, ranking weights, rider rules, actuarial parameters and DMN values (D6) as new versions                                                               | actuarial, compliance |
| 4   | InfoSec review of the hardened OmniRoute (or move to LiteLLM)                                                                                                                      | architecture, InfoSec |
| 5   | Provision model routes and meet every TDD §5.2 gate in `make eval-live`                                                                                                            | engineering           |
| 6   | The reranker decision, then remove the retrieval waivers once the gates are met on the real corpus                                                                                 | project owner         |
| 7   | India-region KMS/HSM behind `KeyService`, in place of the local key-encryption key                                                                                                 | security              |
| 8   | A real RFC 3161 time-stamp authority for the daily anchor                                                                                                                          | security              |
| 9   | mTLS between the orchestrator, domain tier and gateway                                                                                                                             | platform              |
| 10  | Staff SSO for the internal endpoints and the gateway admin surface                                                                                                                 | platform              |
| 11  | Legal sign-off on the retention schedule (D7) and the GST treatment                                                                                                                | legal, tax            |
| 12  | Least-privilege database roles in the deploy environment (the V7 matrix) and a PUT-only MinIO user for anchors                                                                     | platform              |
| 13  | The canary pipeline with two-person sign-off for every governed artefact                                                                                                           | engineering           |
| 14  | An advisor review tool for the pilot (D8); today the hand-off queue is an internal API only                                                                                        | product               |

## Open decisions

| #   | Decision                                                                                                                         | Status                                                                                                                                                         |
| --- | -------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| D1  | The state specification's missing parts (sections 5.4, 5.5, 6 and the constraint catalogue) and confirmation of deviations V1–V7 | **Open** (product owner)                                                                                                                                       |
| D2  | Frontier-model hosting: an India-region managed endpoint with redacted envelopes and a DPA, or self-hosted open-weight only      | **Open** (CISO, DPO); blocks `make eval-live` and every model-dependent metric                                                                                 |
| D3  | AI gateway                                                                                                                       | **Decided**: OmniRoute (own hardened instance); InfoSec acceptance of the TDD §1.6 profile and its HA model still pending, with LiteLLM as the stated fallback |
| D4  | Orchestration runtime                                                                                                            | **Decided 2026-10-02**: Python LangGraph                                                                                                                       |
| D5  | Launch scope                                                                                                                     | **Decided 2026-09-21**: term with riders                                                                                                                       |
| D6  | Suitability thresholds (affordability bands, vulnerability rules)                                                                | **Open** (compliance, actuarial); illustrative 10 % / 20 % bands until then                                                                                    |
| D7  | Retention schedule and session TTL                                                                                               | **Open** (DPO, legal); the build uses configurable values (30-day sessions, 13-month hot store, 397-day key retention)                                         |
| D8  | Advisor review after the pilot                                                                                                   | **Open** (head of distribution); the TDD recommends risk-based review                                                                                          |

## Scalability considerations

The design is stateless at the process level (state lives in Postgres and Valkey), but several choices cap scale today. These follow from the records above.

| Area                 | Today                                                                                                                                                       | Consideration                                                                                                                     |
| -------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Orchestrator         | Stateless apart from per-process caches (bundle, Presidio, catalogue names); concurrency per session is serialised by a Valkey lock and a `NOWAIT` row lock | Horizontal scaling is possible; quote ids, the per-process warm-up cost and the pool size (`SS_PG_POOL_MAX` 20) need review first |
| Gateway              | One OmniRoute Node process with one SQLite writer                                                                                                           | The HA model is a deployment gate (HARDENING.md)                                                                                  |
| Embedding and rerank | CPU TEI: a 120-candidate rerank takes about 80 to 119 s                                                                                                     | Needs GPU or a different serving path to meet 300 ms; the candidate pool is production-sized on purpose                           |
| Postgres             | One instance; audit hot store 13 months                                                                                                                     | Step 33 plans monthly partition drops through `audit_maintenance`                                                                 |
| Audit archive        | Daily anchors only                                                                                                                                          | Step 31 adds the per-event WORM stream, with consumer-lag export                                                                  |
| Rate limits          | 20 turns a minute per subject                                                                                                                               | S2 discovery can take about 26 turns; tune with real traffic                                                                      |
| Evaluation           | Golden sets sized for CI (188 + 19 conversations, 20 Q/E pairs per collection)                                                                              | TDD production sizes are roughly 7× to 50× larger, depending on the set (see [evaluation](evaluation.md#gap-to-the-production-sizes))                                             |

## Longer-range roadmap

The TDD phases (§6.1, an October 2026 start; team of about 13):

| Phase                | Weeks     | Scope                                                | Exit criterion                                                            |
| -------------------- | --------- | ---------------------------------------------------- | ------------------------------------------------------------------------- |
| 0 Foundations        | 1–6       | Infra, contracts, keys, audit, gateway               | InfoSec sign-off; chain verification; retrieval gates met on term corpora |
| 1 Core build         | 7–16      | S0–S3, the invariants, §5.2 gates offline            | I1–I8 property-tested; §5.2 gates met offline                             |
| 2 Supervised pilot   | 17–22     | Advisor approval of S3 responses (D8)                | Advisor override and escaped-defect targets met over 500 sessions         |
| 3 Limited launch     | 23–30     | Traffic 5 %, 25 %, 100 % behind canaries             | §5 targets held 4 consecutive weeks                                       |
| 4 Application states | 31 onward | S4–S8, AML, refund path                              | Per-state gates; legal sign-off on premium timing                         |
| 5 Product expansion  | after 4   | Savings/par, ULIPs, WhatsApp, voice, Consent Manager | Category gates                                                            |

Phase 1's engineering scope is complete on stubs and DUMMY content. Its exit criteria "retrieval gates met" and "§5.2 gates met offline" are **not yet met**; they are the carry-overs at the top of this page (details in [evaluation](evaluation.md)). Phase numbers here follow the TDD §6.1 plan, where the application states S4–S8 are phase 4; the product specification and some TDD sections call S4–S8 "Phase 2".

**Suggested next engineering moves** (each follows from a recorded gap, not from new scope): resolve the reranker decision and recalibrate on a realistic corpus; provision model routes and run `make eval-live`; apply the CI `stack` and `ui` diffs and track `ci.yml`; add the `locale` field and document URLs to the Conversation API; add a Presidio warm-up at start-up; make quote ids unique across instances; schedule an `INTAKE_PENDING` retry sweep; grow the golden, retrieval and red-team sets toward the TDD sizes with compliance-reviewed labels; then Steps 31–33.
