# OmniRoute hardening (TDD §1.6)

SurakshaSetu's model gateway is its own OmniRoute container. It is service `omniroute` in `infra/compose.yaml`, under profile `gateway`, on `127.0.0.1:20130`. It fronts the chat routes only; embed and rerank go straight to TEI (TDD §1.5, decided in Step 8).

**Pinned release:** `diegosouzapw/omniroute:3.8.50@sha256:085c57ad…`, an OCI index for arm64 and amd64. Every statement below was checked against that release's source.

**Where the settings live:**

- **Container environment:** `infra/compose.yaml`. OmniRoute reads these variables at start-up.
- **Everything else:** the gateway's SQLite database, which is set through its admin API. Git holds the values in `seed.json`.
  - `make gateway-up` starts the container and runs `orchestrator/scripts/omniroute_seed.py apply`. That imports the connection, combos and app key, sets each settings section and the key's policy, then reads everything back.
  - `make gateway-verify` does only the read-back. It exits 1 on any drift, because OmniRoute answers 200 to keys it drops.
- **Tests:**
  - `orchestrator/tests/unit/test_omniroute_hardening.py`: checks the Git side, with no network.
  - `orchestrator/tests/stack/test_omniroute_stack.py` and `test_gateway_stack.py`: check the running gateway.
  - A unit test fails if a test named below doesn't exist.

## Rows

| # | TDD §1.6 item | Setting → value | Tests |
| --- | --- | --- | --- |
| 1 | Combos: priority strategy, eval-qualified models only | `import.combos[*].strategy` → `priority`. One target each (`vllm/stub-*`). `config.maxRetries` → 0. | `test_omniroute_hardening.py::test_combos_are_exactly_the_chat_routes_on_the_priority_strategy`, `test_omniroute_stack.py::test_the_running_gateway_matches_the_seed`, `test_gateway_stack.py::test_every_chat_route_round_trips` |
| 2 | Auto aliases, cost-optimised, random and adaptive strategies disabled | `settings.autoRoutingEnabled` → false (the only switch for `auto/*`). `settings.adaptiveVolumeRouting` → false. No combo uses another strategy. The app key's `allowedCombos` is the six chat routes, with `modelAccessMode` → `restricted` and `allowedModels` → []. `disableNonPublicModels` stays false: in 3.8.50 it makes the gateway skip our own `vllm/stub-*` targets as "non-public", and the `auto/*` path it guards is already closed. | `test_omniroute_hardening.py::test_auto_and_adaptive_routing_are_off`, `test_omniroute_hardening.py::test_the_app_key_reaches_only_the_chat_combos`, `test_omniroute_stack.py::test_the_app_key_reaches_only_the_chat_combos` |
| 3 | Compression off; the orchestrator rejects any response whose `x-omniroute-compression` shows it fired | `compression.enabled` → false, `defaultMode` → `off`, `autoTriggerTokens` → 0. The app key's `compressionEnabled` → false. The adapter accepts only an off mode with no `tokens=`/`rules:` segment. | `test_omniroute_hardening.py::test_compression_memory_and_semantic_cache_are_off`, `test_gateway.py::test_a_response_whose_compression_fired_is_rejected`, `test_gateway.py::test_a_compression_header_that_did_not_fire_is_accepted`, `test_omniroute_stack.py::test_a_client_cannot_switch_compression_on` |
| 4 | Memory (keyword and vector recall) off | `memory.enabled` → false, `memory.skillsEnabled` → false (skills would add tools to the request). | `test_omniroute_hardening.py::test_compression_memory_and_semantic_cache_are_off`, `test_omniroute_stack.py::test_the_running_gateway_matches_the_seed` |
| 5 | Semantic cache on the public FAQ route only (Phase 1 has none, so off) | `import.settings.semanticCacheEnabled` → false. This flat key is the one chat reads, and only the import writes it. `database.cache.semanticCacheEnabled` → false (display copy). The app key's `cacheDefaultMode` → `bypass`. | `test_omniroute_hardening.py::test_compression_memory_and_semantic_cache_are_off`, `test_omniroute_stack.py::test_identical_calls_are_never_answered_from_a_cache` |
| 6 | No-auth, web-cookie and free-tier providers removed; egress blocked at the network layer | One connection, the self-hosted stubs. The app key's `allowedConnections` is that connection alone, so keyless providers never get their synthetic credential. `settings.blockedProviders` → the 13 keyless ids and their aliases. `noAuthFallbackDisabledProviders` → the 4 anonymous fallbacks. `OMNIROUTE_EMERGENCY_FALLBACK` → false. **Network egress: deployment gate.** | `test_omniroute_hardening.py::test_the_only_upstream_is_the_self_hosted_stub_connection`, `test_omniroute_hardening.py::test_keyless_providers_are_blocked`, `test_omniroute_stack.py::test_the_app_key_reaches_only_the_chat_combos` |
| 7 | Proxy scopes and client-fingerprint controls disabled | `settings.proxyEnabled` and `perKeyProxyEnabled` → false. `settings.cliCompatProviders` → []. Env `ENABLE_TLS_FINGERPRINT` → false and `ENABLE_SOCKS5_PROXY` → false. | `test_omniroute_hardening.py::test_fingerprinting_and_proxies_are_off`, `test_omniroute_stack.py::test_the_running_gateway_matches_the_seed` |
| 8 | Built-in guardrails (injection checks, PII redaction) on, as a second line | Env `INPUT_SANITIZER_ENABLED` → true, `INJECTION_GUARD_MODE` → `warn`, `PII_REDACTION_ENABLED` → true (your decision, 2026-09-28; see below). | `test_omniroute_hardening.py::test_built_in_guardrails_are_on_as_a_second_line`, `test_omniroute_stack.py::test_the_gateway_masks_pii_as_a_second_line`, `test_omniroute_stack.py::test_an_injection_is_only_flagged_so_the_guard_still_classifies_it` |
| 9 | MCP server and A2A server disabled on the application key; admin scopes only from the ops network | `settings.mcpEnabled` and `a2aEnabled` → false. The app key's `scopes` → `["self:usage"]` (OmniRoute always adds it; no `manage`, `admin` or `mcp:connect`). Env `OMNIROUTE_MCP_ENFORCE_SCOPES` → true. `OMNIROUTE_API_KEY` is never set, because A2A routes with it and would bypass the key's policy. | `test_omniroute_hardening.py::test_mcp_and_a2a_are_off_and_no_server_key_exists`, `test_omniroute_stack.py::test_mcp_and_a2a_refuse_the_app_key` |
| 10 | Admin dashboard behind SSO, INITIAL_PASSWORD set, never public | Port `127.0.0.1:20130` only. `INITIAL_PASSWORD` comes from `OMNIROUTE_INITIAL_PASSWORD`, with a compose dummy default, never CHANGEME. `requireLogin` stays true. **SSO (OIDC): deployment gate.** | `test_omniroute_hardening.py::test_the_dashboard_and_api_listen_on_loopback_only`, `test_omniroute_hardening.py::test_secrets_come_from_the_environment_with_dummy_defaults`, `test_omniroute_stack.py::test_the_admin_api_needs_a_login` |
| 11 | Storage: local SQLite with encrypted credentials; configuration seeded from Git | Env `STORAGE_ENCRYPTION_KEY` comes from `OMNIROUTE_STORAGE_ENCRYPTION_KEY`; unset means plaintext credentials. Volume `omniroute-data`. `seed.json` is applied and verified by `make gateway-up` / `gateway-verify`. **HA model: deployment gate.** | `test_omniroute_hardening.py::test_secrets_come_from_the_environment_with_dummy_defaults`, `test_omniroute_stack.py::test_the_running_gateway_matches_the_seed` |
| 12 | *Added:* every request needs an API key | Env `REQUIRE_API_KEY` → true. It defaults to false, and then even an invalid key is let through anonymously. | `test_omniroute_hardening.py::test_every_request_needs_an_api_key`, `test_omniroute_stack.py::test_a_request_without_a_valid_key_is_refused`, `test_gateway_stack.py::test_a_wrong_key_is_refused_by_the_gateway` |
| 13 | *Added:* no prompt copies in gateway logs (same risk as memory) | The app key's `noLog` → true: without it, request and response bodies are written to `DATA_DIR/call_logs` whatever the log flags say. `database.logs.detailedLogsEnabled` and `callLogPipelineEnabled` → false. | `test_omniroute_hardening.py::test_nothing_copies_prompts_into_gateway_logs`, `test_omniroute_stack.py::test_the_running_gateway_matches_the_seed` |
| 14 | *Added:* the model sees exactly the envelope the audit log records (TDD §1.5) | The upstream is a built-in `vllm` connection, not an `openai-compatible-*` node, which would rewrite `json_schema` into a system message. `settings.customSystemPromptEnabled` → false. Memory and skills are off (row 4). | `test_omniroute_hardening.py::test_the_only_upstream_is_the_self_hosted_stub_connection`, `test_gateway_stack.py::test_the_model_receives_exactly_what_the_adapter_sent` |
| 15 | *Added:* no hidden retries, fallbacks, queues or waits | Every combo has `config.maxRetries` → 0; the default is 1 retry after 2 s, past every route's budget. `OMNIROUTE_EMERGENCY_FALLBACK` → false. `resilience.requestQueue.autoEnableApiKeyProviders` → false: by default every API-key connection is queued at 60 requests a minute, at least 350 ms apart, with waits of up to 15 s. That measured ~340 ms per back-to-back call, over guard-input's 200 ms budget. `waitForCooldown.enabled` and `comboCooldownWait.enabled` → false (waits of up to 30 s). Rate limits belong to the orchestrator (TDD §1.4). | `test_omniroute_hardening.py::test_feature_flags_the_verifier_expects_are_the_compose_values`, `test_omniroute_hardening.py::test_the_gateway_adds_no_queue_or_hidden_waits`, `test_omniroute_stack.py::test_a_failing_model_is_a_gateway_error_without_a_retry` |
| 16 | *Added:* one pinned release | `image: diegosouzapw/omniroute:3.8.50@sha256:…`. | `test_omniroute_hardening.py::test_the_image_is_one_release_pinned_by_digest` |

## Guardrails: why `warn`, and why PII masking is on everywhere

OmniRoute's built-in guardrails are thin: English regular expressions for injection, and PII patterns for email, card numbers, Brazilian CPF/CNPJ/phone and US SSN (no Aadhaar or PAN). The primary rails are the orchestrator's (TDD §4.2, Step 10).

- **Injection guard in `warn` mode** detects and logs but does not block. In 3.8.50, `block` applies to every route and ignores per-request opt-outs. That would stop `guard-input` from ever seeing the attacks it exists to classify, and a system prompt that says "never reveal your system prompt" would be blocked on every call.
- **PII masking is on for every route.** On the redacted routes it is a second pass after Presidio. On the self-hosted routes it masks emails and card- or phone-shaped numbers before the model; no S0–S3 slot needs them.
  - If the masker ever changes a gen-route envelope, the model saw less than the audit log recorded, and only where the first line missed something.

## How OmniRoute 3.8.50 behaves (read in its source, confirmed by the stack tests)

- **`x-omniroute-compression`** is on every chat response: `off; source=off` when compression is disabled. When compression runs, the value is `<mode>; source=<source>`, and rewrite rules append `; tokens=a->b; rules: …`.
  - Upstream `x-omniroute-*` headers never reach the client, so a stub-injected header cannot prove the adapter's rejection end to end.
  - The unit tests prove it with the real formats, and a stack test shows a client can't switch compression on.
- **The response `model`** is the combo target's model id (`stub-guard`), not what the upstream answered and not the combo name. So each combo targets its own stub id, and the stub maps it back to the route.
- **`user` is forwarded unchanged**, so no session marker is needed. The `X-SS-*` headers are not forwarded, and nothing downstream needs them.
- **No fallback count is reported on chat** (`X-OmniRoute-Fallback-Attempts` is set only for image, speech and video), so `GatewayResult.fallback_hops` stays 0.
- **A single-target combo retries once after 2 s by default**, and a failure may cool the connection down for about 3 s.
- **Keys can be imported with a chosen value.** That is how the app key equals `SS_GATEWAY_API_KEY` and never appears in Git. The import resets the key's restrictions, so the seed always re-applies the policy afterwards.

## Not carried by the gateway

`embed` and `rerank` are not combos. OmniRoute hosts no embedding or rerank models, and its `/v1/rerank` speaks the Cohere/Voyage shape. The adapter calls `tei-embed` and `tei-rerank` directly on the self-hosted network (`SS_TEI_EMBED_URL`, `SS_TEI_RERANK_URL`).

## Deployment gates (not provable on a laptop)

- **Egress:** enforce the provider allowlist at the network layer; don't rely on gateway configuration alone (TDD §1.6).
- **Admin access:** SSO (OIDC) in front of the admin UI and API, reachable from the ops network only.
- **HA:** 3.8.50 is one Node process writing one SQLite file, so there are no replicas or rolling restarts. Confirm the HA model before production.
- **Supply chain:** build the image internally from the pinned release and scan its SBOM.
- **Real secrets:** replace the compose dummies (`OMNIROUTE_*`) and `SS_GATEWAY_API_KEY`.
- **Production routes:** the model pools and providers in `routes.prod.example` (D2). Each is a combo target that passes the evaluation gate end to end through the gateway.

## Upgrading the image

When bumping the pinned release, re-check each of these in the new source, and rerun `make gateway-up`, `make gateway-verify` and both stack tests:

- the keyless provider list (`src/shared/constants/providers/noauth.ts`) against `settings.blockedProviders`;
- the compression header format;
- the response `model` rewrite;
- the flat `semanticCacheEnabled` key;
- whether `vllm` still passes `json_schema` through.
