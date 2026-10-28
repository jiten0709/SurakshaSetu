# SurakshaSetu

An AI-assisted conversational advisor for life and term insurance in India. It guides a customer from consent, through eligibility and needs discovery, to a compliant, explainable plan recommendation, and then hands the chosen plan to the insurer's existing application journey.

> **Phase 1 is complete (Steps 1–30).** The whole S0–S3 journey works end to end in the terminal client and in the browser: consent, eligibility, the Quote-Only path, needs discovery, a ranked and cited recommendation with hash-verified disclosures, and a signed hand-off. The cross-cutting handlers (FAQ, objections, human escalation, pause, data erasure) work too.
>
> - **Evidence:** `make eval` passes 208 of 208 (207 golden and red-team conversations plus the pilot DUMMY gate), and 0 of 157 attacks succeed. Compliance can render a verified dossier for any session.
> - **Not real yet:** every product, rule, rate and document is fictitious `DUMMY` data, and the language models are local stubs until the hosting decision (D2).
> - **Upcoming:** the production layer (Steps 31–33: WORM audit streaming, observability, retention) and the pre-pilot gates. See the [roadmap](documentation/roadmap.md).

## Core idea

- **The LLM handles language only.** It interprets what the customer says and explains results.
- **Every decision is deterministic.** Eligibility, suitability, cover sizing, premiums, ranking and state transitions come from versioned rule services, so an auditor can reproduce any recommendation from its inputs.
- **Legally significant text comes from templates.** Consent notices, disclosures and hand-off scripts are never generated, and every mandatory disclosure is recorded in a hash-chained audit ledger.
- **Consent comes first.** No data is stored and no service is called before the customer consents, and a withdrawal takes effect in the same turn.

## Quick start

### 1. Install the prerequisites

| Tool | Why it's needed |
| --- | --- |
| [Docker](https://docs.docker.com/get-docker/) | Runs the whole stack: Postgres, Qdrant, MinIO, the model servers, the gateway, the API and the UI. Give the Docker VM at least 8 GB of memory. |
| [uv](https://docs.astral.sh/uv/) | Runs the Python setup steps on your machine (migrations, gateway seeding, knowledge-base ingest). |
| JDK 21 | Builds the domain services (eligibility, suitability, quotes, ranking). |

You don't need Node on your machine: the browser UI runs in its own container.

### 2. Start everything

```sh
make start
```

This one command takes the machine from any state to a running demo. It starts the stack, then seeds the gateway, catalog and knowledge base only if they are missing, then starts the API, the timers and the UI.

- **First run:** about 40 minutes, mostly downloads (container images and about 2.9 GB of model weights).
- **Later runs:** seconds to a minute or so, because nothing is reseeded.

### 3. Chat in the browser

Open **http://127.0.0.1:5173**. The first start installs the UI's dependencies inside its container, which takes about a minute.

The models are local stubs, so stick to the quick-reply buttons and short answers such as `34`, `411001`, `no` or `12 lakh`.

### 4. Or chat in the terminal

```sh
cd orchestrator && uv run python scripts/chat.py      # add --locale hi-IN for Hindi
```

| Type | To |
| --- | --- |
| a number | pick a quick reply |
| `f` | fill in the consent form |
| `/erase` | delete your data |
| `/quit` | leave |

### 5. Prove it works (optional)

```sh
make down && make local-setup
```

This runs the full verification from TDD §7.5: the evaluation sets, the invariant property tests, the scripted S0 → S3 conversation ending in a signed hand-off, audit-chain verification and the pilot `DUMMY` release gate. It refuses to run while the stack is up, so `make down` comes first. Run `make start` again afterwards to bring the app back.

### 6. Stop

```sh
make down
```

This stops every container and keeps your data in the Docker volumes, so the next `make start` is quick.

> **Developing the API or UI?** Run them on your machine instead with `make serve` and `make ui`, each in its own terminal. Stop the compose services first (`make down`, then `make up && make gateway-up`), because they use the same ports, 8000 and 5173.

## Documentation

Start with the [overview](documentation/overview.md), which maps every document: architecture, state machine, data and audit, subsystems, API reference, operations, evaluation, design decisions, the roadmap and a glossary. Release sign-off is in [`RELEASING.md`](RELEASING.md) and gateway hardening in [`infra/omniroute/HARDENING.md`](infra/omniroute/HARDENING.md).

All reference data is fictitious, including product UINs, disclosure sets and knowledge-base content.
