# Design decisions

The patterns the system is built on, the decisions that shaped it, how it fails, and what each choice cost.

## Architectural patterns

| Pattern                                                         | Where it appears                                                                                                                                                                               | Why                                                                                                                         |
| --------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| **Deterministic core, language-only model**                     | Every number, rule and transition comes from the Java tier or the pure FSM; the model interprets and explains                                                                                  | Lets an auditor reproduce any recommendation from inputs and rule versions; removes the largest class of hallucination risk |
| **Contract-first, generated on both sides**                     | Two OpenAPI specs; Python models and Java interfaces are generated; drift checks in `make check`                                                                                               | One source of truth; a change to a shape fails the build until both sides agree                                             |
| **Pure function state machine as data**                         | `fsm/rows.py` (rows with explicit order), `fsm/transition.py`; LangGraph edges only call it                                                                                                    | Exhaustively testable: Hypothesis properties, a one-field sweep, a table test per row, a non-vacuity test                   |
| **Content as versioned, hash-locked, owner-assigned artefacts** | Prompt bundles, lexicon packs, DMN, params, weights, rate table, FAQ, disclosure sets, KB snapshots                                                                                            | Legal and actuarial text changes by a new version through a gated pipeline, never by editing in place                       |
| **Version pinning per session**                                 | `pins` on `conv.session` and every audit event; `load_pinned`; rules `pins.rules` on each request                                                                                              | A live conversation never changes behaviour under the customer (I7)                                                         |
| **Hash-chained event log, commit before release**               | `audit/`, one transaction per turn, release after commit                                                                                                                                       | Proves what happened, and nothing was said that was not recorded (I8)                                                       |
| **Crypto-shredding**                                            | Per-subject data keys, `erasure_rw`, `destroy_after`                                                                                                                                           | Reconciles immutable audit with the right to erasure                                                                        |
| **Ports and adapters**                                          | `RatingEngine` port with a stub adapter; `KeyService` protocol; `Gateway` adapter; the `Tei` protocol; the journey POST                                                                        | Real rate tables, KMS, model routes and the journey replace stubs without touching callers                                  |
| **Template-first composition**                                  | Consent, disclosures, scripts and crisis text are rendered from templates; generation only writes a lead sentence or a cited narrative                                                         | Legally significant text is never generated                                                                                 |
| **Defence in depth**                                            | Input rails, grounding rails, release checks, gateway guardrails (second line), a compliance-owned lexicon                                                                                     | No single layer is trusted                                                                                                  |
| **Fail closed where harm is possible, degrade where it is not** | Self-harm, RC-GUARD, NLI, Redis and consent outages fail closed; a domain outage in S1 falls back to a template                                                                                | See the table in 6.3                                                                                                        |
| **Idempotency and replay**                                      | `Idempotency-Key` per turn, byte-identical replays, idempotent consent creation, timer keys                                                                                                    | Safe retries after any 503                                                                                                  |
| **Enforced module boundaries**                                  | `retrieval` never imports `domain`; nothing under `domain/` or named for disclosures, premiums, placeholders or quotes imports `retrieval`; `fsm` imports only the stdlib, typing and pydantic | Keeps explanation and numbers from ever mixing                                                                              |
| **Mutation-proven gates**                                       | The red-team judge, the eval gates and the FSM tests were each proved by deliberately breaking the guarded behaviour                                                                           | A gate that cannot fail proves nothing                                                                                      |
| **Thin client**                                                 | `frontend/` renders server parts and sends server actions unchanged                                                                                                                            | No second implementation of any rule                                                                                        |

## Decision log

Decisions that shaped the build. A decision id (D3, D4, D5, OD-1), where one exists, is part of its name.

| Decision | Choice and reason |
| --------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Source-of-truth order | TDD, then the implementation guide's recorded resolutions, then the repository as built. Prompts summarise but never replace the TDD, because Step 2's reconstructed DDL came out wrong |
| Launch scope (D5) | Term plans with riders |
| AI gateway (D3) | OmniRoute, but SurakshaSetu's **own** hardened instance by digest; an earlier general-purpose instance failed several hardening rows |
| Orchestration runtime (D4) | Python LangGraph rather than LangGraph4j |
| Disclosure key (OD-1) | Key `catalog.disclosure` by `(disclosure_id, language)` (migration V8); changed text needs a new id and registry version |
| One hashing scheme | RFC 8785 JCS for every hash, pinned by shared test vectors in both languages |
| Audit hash scope | Hash every non-personal column, not only `header`, so edits to `fsm_state`, `pins` or `occurred_at` are detected |
| Embed and rerank bypass the gateway | Query text stays self-hosted; OmniRoute hosts no embedding or rerank models |
| Running reranker | gte-multilingual-reranker-base: meets the Hinglish abstention bar the alternative misses; final choice still open (see [evaluation](evaluation.md#retrieval-evaluation)) |
| Withdrawal and under-18 → Data Erasure from every state, including S0 | Departs from TDD §3.9's "Exit"; even pre-consent text exists and a child's data should not be kept |
| Terminal states are closed | A withdrawal on a closed session is routed to erasure outside the FSM |
| Guards G1–G4 precede FAQ and objection | A FAQ must never keep a lapsed or stale session in place |
| Lock and rate limit before the graph | LangGraph writes a checkpoint even when a node refuses |
| Dependency down → Pause (`CC3`) | A Suitability or ranker outage pauses rather than fails the customer |
| Evaluate, then bind (S2) | An earlier plan had the order reversed; the summary needs the engine's numbers before the customer confirms |
| `excluded_uins` = kill-switched only | Otherwise "at most two rediscovery loops, then a person" is unreachable with two launch products |
| Output lexicon is a deploy-time setting | Not a session pin; every verdict records the pack version |
| No ragas library for faithfulness | RAGAS _definitions_ with `verify-claims` as the judge; reconsidered once real routes exist |
| Latency classes | S1/Quote-Only/S2 ≤ 2.5 s; S3 and side-query ≤ 6.5 s; other states reported only |
| Waivers have no expiry | A waiver holds until compliance removes it; `NEVER_WAIVED` covers suites, injection success and hallucination gates |
| Frontend is a thin client with a hand-written contract | The routes return pre-serialised canonical bytes FastAPI cannot type |
| Proposer ≠ life assured | The table runs on the proposer's age first; complexity escalates; otherwise the life assured's result applies |
| Cover term `n` | `min(longest dependency, cover_to_age − age)`; spouse and parent depend until `cover_to_age` (TDD §3.7 updated) |
| No DUMMY product is rated by gender | S1 never asks gender |

## Fail-closed versus degrade

| Component                | When it fails               | Behaviour                                                                                        |
| ------------------------ | --------------------------- | ------------------------------------------------------------------------------------------------ |
| Self-harm detection      | `guard-input` unavailable   | **Closed**: a local lexicon hit still routes to Safety; a clean turn is not blocked              |
| Output `RC-GUARD`        | Guard unavailable           | **Closed**: nothing is released; `release_blocked` + `advisor_offer`                             |
| Claim check (`GR-NLI`)   | `verify-claims` unavailable | **Closed**: the draft is held and the template renders                                           |
| Redis (lock, rate limit) | Down                        | **Closed**: 503                                                                                  |
| Consent Service          | Outage during S0            | **Closed**: `consent_retry`, never proceeds                                                      |
| Consent Service          | Outage during erasure       | **Open on purpose**: erasure goes on, the withdrawal is recorded as pending and a sweep retries  |
| Domain tier in S1/S2     | Timeout, unreachable or 5xx | **Degrade**: the node answers from a template (`screening_retry`, or Pause via CC3 in S2 and S3) |
| Disclosure Registry      | Hash mismatch               | **Closed**: `500 REGISTRY_INTEGRITY`, the set is never served                                    |
| Retrieval                | Embed down / rerank down    | **Degrade**: BM25-only search, stricter threshold; still abstains if the evidence is weak        |
| Model route              | Down or over budget         | **Degrade**: deterministic card or template; no retry                                            |

## Trade-offs

| Choice                                                                | Gained                                                         | Cost                                                                                                              |
| --------------------------------------------------------------------- | -------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Hash-locked prompt bundles                                            | Provenance and I7                                              | Every wording change is a new version and a retired predecessor (nine bundles in three weeks)                     |
| Java rule tier beside a Python conversation tier                      | Actuarial and compliance change tables without touching Python | Two languages, a contract, two JCS implementations kept in step by vectors                                        |
| One database transaction per turn, release after commit               | Atomicity and I8                                               | The session row stays locked for the whole turn; a concurrent request gets 409 `SESSION_BUSY` rather than waiting |
| LangGraph checkpoint after the commit, hydrated from official records | The checkpoint can never be ahead of the audit trail           | A lagging checkpoint costs a rebuild; engine payloads are recomputed                                              |
| Checkpoint stores `SessionState` in clear                             | Simple, fast resume                                            | Personal data lives in `langgraph.*` until the erasure hard-delete (open item)                                    |
| Own OmniRoute instance                                                | Hardened, pinned, reproducible                                 | One Node process and one SQLite writer: no high-availability model yet                                            |
| Direct TEI calls                                                      | Query text never leaves the network                            | No gateway fallback for embed or rerank                                                                           |
| Immutable KB snapshots                                                | Reproducible answers for a pinned session                      | A re-index for every content change; old points remain                                                            |
| Stub models in every gate                                             | Deterministic, repeatable evaluation                           | Model-dependent metrics (faithfulness, slot F1, critical-intent recall) cannot be judged until D2                 |
| Template-first                                                        | Auditable, translatable by approved copy                       | Less fluent; Hindi needs approved translations before any pilot                                                   |
| Thin client                                                           | No second rule implementation, small attack surface            | A UI need the contract lacks (for example a locale in the turn response) is a backend change request              |
| A 20-turns-a-minute rate limit                                        | Cheap abuse protection                                         | S2's discovery can reach about 26 turns, so the limit is a known tuning item                                      |
