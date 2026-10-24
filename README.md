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

Prerequisites: [uv](https://docs.astral.sh/uv/), Docker, and JDK 21.

```sh
make start        # everything, first run included: the stack, gateway, catalog and knowledge base
                  # (each only if missing; first run: ~40 min of downloads), then the Conversation API
                  # (127.0.0.1:8000), the timers and the browser UI (127.0.0.1:5173)
```

To prove the system end to end (eval sets, invariant tests, the scripted S0-S3 conversation, audit verification and the pilot DUMMY gate), run `make local-setup`; it refuses a running stack, so `make down` first.

Then open the browser UI at http://127.0.0.1:5173 (the first start installs the UI's dependencies inside its container, about a minute), or chat in the terminal:

```sh
cd orchestrator && uv run python scripts/chat.py            # --locale hi-IN for Hindi
```

(type a number to pick a quick reply, `f` to fill the consent form, `/erase` to delete your data, `/quit` to leave). To run the API and the UI on the host instead, use `make serve` and `make ui` (each in its own terminal, with `make app-up`'s services stopped: they share ports 8000 and 5173).

## Documentation

Start with the [overview](documentation/overview.md), which maps every document: architecture, state machine, data and audit, subsystems, API reference, operations, evaluation, design decisions, the roadmap and a glossary. Release sign-off is in [`RELEASING.md`](RELEASING.md) and gateway hardening in [`infra/omniroute/HARDENING.md`](infra/omniroute/HARDENING.md).

All reference data is fictitious, including product UINs, disclosure sets and knowledge-base content.
