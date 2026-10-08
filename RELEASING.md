# Releasing a change that can alter behaviour

TDD §4.5: anything that can change what SurakshaSetu says or decides ships through one pipeline. That covers prompt bundles, DMN rules and parameters, lexicon packs, corpus snapshots, disclosure sets and model routes. Each release needs three things:

- its automated gates;
- a two-person sign-off;
- a 5% canary.

Each release also leaves a CONFIG_RELEASE audit event. Live sessions keep the versions they pinned (I7). Only a kill switch moves them.

This file describes the pipeline as built (Step 23) and says what still has to be set up outside the repository.

## 1. Two-person sign-off

The repository encodes **who** must sign off. The repository's settings make it binding.

- **`CODEOWNERS`** (repository root) names the owners of every governed path. For now it has one handle, and the comment above each rule names the roles that rule stands for:

  | Path | Roles |
  | --- | --- |
  | `content/prompt-bundles/` | compliance and the product owner |
  | `content/lexicon/` | compliance |
  | `content/faq/` | compliance with the DPO |
  | `content/seed/catalog/` (disclosure sets) | legal and compliance |
  | `content/seed/consent/` | legal and compliance |
  | `content/seed/kb/` | compliance |
  | `domain-services/.../dmn/`, `params/`, `ranking/` | actuarial and compliance |
  | `.../rating/` | actuarial, compliance and tax |
  | `infra/omniroute/` | architecture and InfoSec |

- **Branch protection on `main`** (a repository admin sets it; nothing in Git can):
  - require a pull request, with **2 approvals**;
  - require review from Code Owners;
  - dismiss stale approvals on new commits;
  - require the status checks `python`, `java` and `stack` (the stack job runs `make eval`);
  - no bypass for administrators.

Two approvals from the owning roles are the TDD's two-person sign-off. Until each role has its own team, the single handle cannot give the second approval on its own changes, so a second reviewer must be added.

## 2. The gates per artefact

| Artefact | Where it lives | Automated gate | Sign-off |
| --- | --- | --- | --- |
| Prompt bundle | `content/prompt-bundles/<version>/` (hash-locked manifest), `SS_PROMPT_BUNDLE` | `make check` (bundle loader and template tests), then `make eval`: golden conversations per state, the red-team suite, and the §5.2/§5.3 gates | Compliance and the product owner |
| DMN rules, actuarial parameters | `domain-services/src/main/resources/dmn/`, `params/` (a new version beside the old, never an edit) | `make check-java` (decision-table tests), `make test-invariants`, `make contract-test`, `make eval`. The TDD's back-test on last month's sessions needs production traffic: a pilot item. | Actuarial and compliance |
| Ranking weights, rate table | `.../ranking/`, `.../rating/` | `make check-java` (`CommercialFieldBanTest` among them), `make eval` | Actuarial and compliance; tax for the rate table's `gst_rate` |
| Disclosure sets | `content/seed/catalog/disclosures.yaml` (changed text needs a new id and a new registry version) | The seed loader refuses changed bodies; the registry recomputes every hash; `make eval` checks I4 and disclosure completeness. The legal-text diff and the translation sign-off are human reviews. | Legal and compliance |
| Corpus snapshot | `content/seed/kb/` and `review-manifest.yaml` (a new snapshot id for any change) | `make kb-ingest`, `make kb-verify`, `make eval-retrieval` (recall@8 and precision@8 per collection), supersession checks | Compliance |
| Model or route change | `infra/omniroute/seed.json`, `infra/compose.yaml` (TEI pins), `SS_EVAL_PRIMARY_MODELS` | `make gateway-verify`, then `make eval-live`: every §5.2 gate through OmniRoute, plus the per-route latency the report lists. The cost check is a deployment item. | Architecture and InfoSec, with compliance |
| Lexicon pack | `content/lexicon/output-lexicon-<version>.yaml` (a new file per change), `SS_OUTPUT_LEXICON` | `make check` (`test_lexicon.py`), `make eval`: the red-team suite is the flagged set, the golden conversations the clean set | Compliance |

**`make eval`** (CI, stub models) enforces:

- the golden and red-team suites, with 0 attack successes;
- retrieval recall and precision, and abstention;
- citation coverage in S3 and side-queries;
- zero unsupported premiums and disclosures;
- the §5.3 gates: consent integrity, disclosure completeness, withdrawal honoured, audit integrity;
- latency p95 per state;
- the golden coverage floor.

It computes the model-dependent metrics against the stub but does not gate on them. **`make eval-live`** enforces every §5.2 gate on provisioned routes, and exits 2 until D2 provisions them. Both write `reports/eval-<ts>.json` and `.md`.

### Waivers

A gate that the DUMMY seed content cannot meet can be waived in `content/eval/waivers.yaml` (owner: compliance):

- each waiver names its gate, the reason and the owner;
- the gate is still computed, and every report shows it as WAIVED;
- a waiver has no expiry date: it holds until compliance removes it from the file, and the gate then blocks again (decided 2026-10-08);
- the §5.3 compliance gates, red-team successes and the conversation suites can never be waived (`eval/metrics.NEVER_WAIVED`).

Today's waivers cover the retrieval recall and precision gates of kb_regulatory and kb_tax (Step 12 X1, the reranker decision).

## 3. Activation and the audit record

- **Prompt bundles** record a CONFIG_RELEASE on the system audit chain when they are activated (`compose.bundle.activate`, run at start-up). The event carries the bundle's manifest hash and approvers. A recorded version is immutable: any change is a new version. The previous version is retired by a prompt-bundle kill switch, which re-pins its sessions (I7's exception).
- **Rules, parameters, ranker and rating versions** are pinned per session and stamped on every ENGINE_DECISION. A CONFIG_RELEASE for their activation belongs to the deployment pipeline (not built yet).
- **Corpus snapshots** are rows in `catalog.corpus_snapshot`, pinned per session. **Lexicon packs** are named on every lexicon GUARD_VERDICT.
- **Kill switches** (`POST /internal/kill-switches`, ops role) act at once, for a product, a prompt bundle or a route, and are audited as KILL_SWITCH. They are the only exception to I7.

## 4. The 5% canary

The 5% canary is a deployment-time item, outside this repository. A release first serves 5% of new sessions. The rest stay on the previous versions, and sessions already started never move. It is promoted when the §5 metrics hold on live traffic. Live sessions keep their pins, so a canary never changes a conversation in progress.

## 5. Experiments

Experiments are allowed **only on non-legal wording**, such as question phrasing and bridge lines (TDD §5.4). Consent copy, disclosures, suitability rules and ranking weights are never A/B tested or tuned by bandits. They change only through the governed releases above.

- Experiment configs live in `content/experiments/` (owner: product and compliance).
- `tests/unit/test_governance.py` fails the build if any config refers to the consent notice or consent copy, a disclosure, the suitability rules or parameters, or the ranking weights.
- Nothing runs experiments yet: the canary pipeline would read the active configs.

## 6. Release checklist

1. A pull request with the change, its new version where the artefact is versioned, and the evidence: `make check`, plus the gate the table names.
2. A green CI run, including the stack job's `make eval` (its report is a build artifact).
3. Two approvals from the owning roles (CODEOWNERS).
4. Merge. At deployment: activation (CONFIG_RELEASE), the 5% canary, promotion; then the old version is retired by a kill switch where the artefact has one.
