# API reference

The two contracts: the OpenAPI specification between the conversation tier and the domain tier, and the Conversation API that chat clients use. Both specifications live in `contracts/openapi/` and are the source of truth; this page explains how they are used.

## The tier contract

The two tiers meet at one machine-readable contract, `contracts/openapi/domain-services.v1.yaml` (OpenAPI 3.1.0, 23 operations under 9 tags, 51 KB). It is the single source of truth for every orchestrator-to-domain shape.

- **Generated on both sides.** `make contracts` regenerates `orchestrator/src/surakshasetu/domain/models.py` (pydantic v2, via `datamodel-code-generator`); Maven regenerates one `<Tag>Api` interface per tag and its models into `target/` on every build. A controller implements its interface; an operation it has not overridden answers 501 problem+json. No DTO is hand-written, and `make check-contracts` fails if the committed Python models drift from the spec.
- **Shared hashing vectors.** RFC 8785 JSON Canonicalization (JCS) is used for every hash. `crypto/jcs.py` (Python) and `common/Jcs` (Java) are the only classes that hash JSON, and `content/testvectors/jcs/` (12 vectors) and `content/testvectors/needs/` (3 vectors) pin both implementations to the same bytes.
- **Decision envelope.** Eligibility, suitability, ranking and quote requests carry `pins.rules`; an unknown version is `409 RULES_VERSION_UNKNOWN`. Responses carry `decision_id`, their version, `inputs_sha256` and `reason_codes` (plus `rule_ids`, `rules_version`, `params_version` for the two rule engines).
- **Errors** are `application/problem+json` with a stable `code` (`UNAUTHORIZED`, `FORBIDDEN`, `NOT_FOUND`, `RULES_VERSION_UNKNOWN`, `IDEMPOTENCY_KEY_REUSED`, `CONSENT_WITHDRAWN`, `PRODUCT_WITHDRAWN`, `NOTICE_MISMATCH`, `P1_REQUIRED`, `TOBACCO_UNDISCLOSED`, `QUOTE_OUT_OF_BOUNDS`, `REGISTRY_INTEGRITY`, `NOT_IMPLEMENTED`, plus the HTTP status names for framework errors).
- **Money** is a decimal string (`^\d+(\.\d{1,2})?$`); only `need_inr` is signed.
- **Auth.** Every `/v1` operation needs `Authorization: Bearer <service token>`; the product kill switch also needs `X-Internal-Token`. Only the orchestrator's ops endpoint holds the internal token. mTLS replaces this in production (a [pre-pilot gate](roadmap.md#release-gates-before-any-pilot-traffic)).
- **`DomainClient`** (`domain/client.py`) never retries. Whole-call budgets: 300 ms for decisions, 150 ms for everything else. Failures raise `DomainError(code, status)`, with a `None` status for `TIMEOUT` and `UNAVAILABLE`. A unit test keeps the client's operation labels equal to the spec's 23 operationIds.

The hashing rules (stated in the spec's `info.description`) are what make a recommendation reproducible:

| Hash                       | Definition                                                                                                                                                 |
| -------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `body_sha256`              | SHA-256(UTF-8(NFC(body)))                                                                                                                                  |
| `set_sha256`               | SHA-256(JCS({registry_version, uin, channel, language, items: [{disclosure_id, body_sha256}]}))                                                            |
| `inputs_sha256`            | SHA-256(JCS(the request JSON exactly as sent)); `common/RawJson` keeps each request body before Jackson binds it, so a re-serialised model is never hashed |
| Suitability exception (I2) | `inputs_sha256` = SHA-256(JCS(needs minus `slots_sha256`)), and it must equal `needs.slots_sha256`                                                         |

The chat-client contract is separate and hand-written, because the routes return pre-serialised canonical bytes that FastAPI cannot type: `contracts/openapi/conversation-api.v1.yaml` (4 operations, 25 action types, SSE events, 11 problem codes). It is linted by `make check-contracts`, validated against every response and event in the golden harness, and used to generate the frontend's TypeScript types. See [Conversation API: actions, parts and problems](#conversation-api-actions-parts-and-problems).

## The Conversation API

Four public routes (`api/routes.py`, contract `conversation-api.v1.yaml`). Every error is `application/problem+json` with `{type, title, status, code}`, never exception text or input.

| Route                                                                                                    | Behaviour                                                                                                                                                                                                                                                                                                                                                                                     |
| -------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `POST /v1/sessions` `{channel: web\|app, locale: en-IN\|hi-IN}`                                          | 201 `{session_id, session_token, events_url}`. Records the **pins**: prompt bundle, the domain tier's rules version (plus params, ranker and registry versions), the active corpus snapshot per collection, and the consent notice in force for the locale. The token is stored only as a SHA-256. `expires_at` = now + 30 days. The only domain calls before consent are two reference reads |
| `POST /v1/sessions/{id}/turns` (Bearer, `Idempotency-Key` UUID) `{text}` xor `{action: {type, payload}}` | 200 with the released body `{turn_id, state, message{text, parts, citations, sources, disclosures, cta, form, quick_replies}, documents}`. A wrong token and an unknown session are the same 401; an expired session is 410                                                                                                                                                                   |
| `DELETE /v1/sessions/{id}`                                                                               | The `ERASE` action as a turn; a repeat is 401                                                                                                                                                                                                                                                                                                                                                 |
| `GET /v1/sessions/{id}/events`                                                                           | SSE: `turn.status` (`analysing`, `composing`, `validating`) and `turn.released` (the whole body, after the commit). Redis pub/sub; nothing is replayed                                                                                                                                                                                                                                        |

Problem codes the four routes send: `BAD_REQUEST` (400), `UNAUTHORIZED` (401), `SESSION_BUSY` (409), `SESSION_EXPIRED` (410), `REQUEST_ENTITY_TOO_LARGE` (413; body limit 16 KiB), `RATE_LIMITED` (429; default 20 turns a minute, 200 an hour), `INTERNAL_SERVER_ERROR`, `BAD_GATEWAY`, `SERVICE_UNAVAILABLE`, `NOT_FOUND`, `METHOD_NOT_ALLOWED`. Redis down, a database error, or a domain or gateway outage before the commit is 503 and safe to retry.

Internal endpoints use a bearer key **per role** (a key for another role is 403): `compliance` reads `GET /internal/audit/sessions/{id}/verify` and `/events` (headers only, never payloads); `advisor` reads `GET /internal/handoffs?queue=` and `/internal/handoffs/{id}` (the decrypted briefing; 404 unknown or erased, 410 `GONE` once the key is destroyed); `ops` posts `/internal/kill-switches {kind, target, active, reason_code}` (a product switch first calls the domain tier's kill switch, then writes the `conv.kill_switch` row and a `KILL_SWITCH` event in one transaction; deactivating a product is 422 `KILL_SWITCH_IRREVERSIBLE`).

## Domain-services operations

The 23 operations of `contracts/openapi/domain-services.v1.yaml`. All need `Authorization: Bearer <service token>`; `setProductKillSwitch` also needs `X-Internal-Token`.

| Tag         | operationId               | Method and path                                  |
| ----------- | ------------------------- | ------------------------------------------------ |
| meta        | `getVersions`             | `GET /v1/meta/versions`                          |
| consent     | `getCurrentConsentNotice` | `GET /v1/consent/notices/current`                |
| consent     | `getConsentNotice`        | `GET /v1/consent/notices/{notice_version}`       |
| consent     | `createConsentRecord`     | `POST /v1/consent/records`                       |
| consent     | `getConsentRecord`        | `GET /v1/consent/records/{consent_id}`           |
| consent     | `changeConsentPurpose`    | `POST /v1/consent/records/{consent_id}/purposes` |
| consent     | `withdrawConsent`         | `POST /v1/consent/records/{consent_id}/withdraw` |
| catalog     | `listProducts`            | `GET /v1/catalog/products`                       |
| catalog     | `getProduct`              | `GET /v1/catalog/products/{uin}`                 |
| catalog     | `getRider`                | `GET /v1/catalog/riders/{uin}`                   |
| catalog     | `setProductKillSwitch`    | `POST /v1/catalog/products/{uin}/kill-switch`    |
| disclosure  | `getDisclosureSet`        | `GET /v1/disclosures/sets/{uin}`                 |
| disclosure  | `getDisclosure`           | `GET /v1/disclosures/{disclosure_id}`            |
| reference   | `getPincode`              | `GET /v1/reference/pincodes/{pincode}`           |
| reference   | `searchOccupations`       | `GET /v1/reference/occupations`                  |
| reference   | `getOccupation`           | `GET /v1/reference/occupations/{code}`           |
| eligibility | `getRequiredAttributes`   | `GET /v1/eligibility/required-attributes`        |
| eligibility | `evaluateEligibility`     | `POST /v1/eligibility/evaluate`                  |
| suitability | `getRequiredSlots`        | `GET /v1/suitability/required-slots`             |
| suitability | `evaluateSuitability`     | `POST /v1/suitability/evaluate`                  |
| ranking     | `rankOptions`             | `POST /v1/ranking/rank`                          |
| quotes      | `createQuote`             | `POST /v1/quotes`                                |
| quotes      | `createQuoteAlternatives` | `POST /v1/quotes/alternatives`                   |

## Conversation API: actions, parts and problems

### C.1 The 25 action types

A client sends `{action: {type, payload}}` in a turn. The contract's `Action` is a `oneOf` of 15 schemas with a discriminator; `SimpleAction` covers the 11 types without a payload. The server also accepts and ignores any unknown type (recorded, not changed).

| Type                                          | Scope                                                                                    | Payload                                                                |
| --------------------------------------------- | ---------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| `START`                                       | Chat opens (any first message also gets the greeting)                                    | none                                                                   |
| `RETRY`, `CONTINUE`, `HUMAN_REQUEST`, `ERASE` | Offered as a reply's own quick replies; `ERASE` is the data-erasure request in any state | none                                                                   |
| `OPT_IN`, `SATISFIED`                         | Quote-Only                                                                               | none                                                                   |
| `REVISE`                                      | S3 call to action                                                                        | none                                                                   |
| `SAVE`                                        | S3 call to action; treated as a deferral in S1, Quote-Only and S2                        | none                                                                   |
| `REJECT_ALL`                                  | S3                                                                                       | none                                                                   |
| `END`                                         | S1, Quote-Only, S2, S3 (objection handler, row CC5b)                                     | none                                                                   |
| `NOTICE_LANGUAGE`                             | S0 while the form is open                                                                | `{language}`                                                           |
| `LANGUAGE`                                    | After S0                                                                                 | `{locale}`                                                             |
| `CONSENT_SUBMIT`                              | S0                                                                                       | `{purposes: {P1, P2, P3}, age_18_plus, notice_version, notice_sha256}` |
| `INTENT`                                      | S0                                                                                       | `{intent}`                                                             |
| `SLOT`, `REASK`, `CONFIRM`                    | S1, Quote-Only, S2                                                                       | `{slot, value}`, `{slot}`, `{confirmed}`                               |
| `FACT`                                        | After a side question                                                                    | `{slot, value, confirmed}`                                             |
| `PERIOD`, `ELECT`                             | S2                                                                                       | period answer; `{elected}`                                             |
| `APPLY`                                       | S3                                                                                       | `{uin?, sum_assured_inr?, term_years?, ppt?, rider_uins?}`             |
| `CHEAPER`                                     | S3                                                                                       | `{uin}`                                                                |
| `DISCLOSURE_ACK`                              | S3                                                                                       | `{uin, registry_version, disclosure_set_sha256, document_sha256}`      |
| `ADVISOR_CONTACT`                             | Human Escalation                                                                         | `{granted}`                                                            |

### C.2 Part identifiers

`message.parts` are ordered `(id, text)` pairs. The UI picks a view by id and treats an unknown id as a paragraph.

| Namespace                                                    | Example                      | Meaning                                       |
| ------------------------------------------------------------ | ---------------------------- | --------------------------------------------- |
| `template:<id>`                                              | `template:RL-S1-AGE`         | A bundle template, verbatim                   |
| `registry:DISC-…`                                            | `registry:DISC-GLOBAL-AI-06` | A disclosure body from the registry, verbatim |
| `notice:<version>`                                           | `notice:2026.09.1-en`        | The consent notice body                       |
| `faq:<version>:<id>`                                         | `faq:2026.10.1:PF-ERASURE`   | An approved privacy FAQ answer                |
| `generated:narrative` / `generated:answer`                   |                              | Model-written, cited text                     |
| `option_card:<UIN>`                                          |                              | A ranked option                               |
| `disclosures:<UIN>`                                          |                              | That option's complete disclosure set         |
| `needs_recap`, `comparison`, `why_it_fits`, `cta`, `sources` |                              | Composer parts                                |

### C.3 Events and problems

SSE events: `turn.status` (`analysing`, `composing`, `validating`) and `turn.released` (the full `TurnResponse`). Problem codes: `BAD_REQUEST`, `UNAUTHORIZED`, `NOT_FOUND`, `METHOD_NOT_ALLOWED`, `SESSION_BUSY`, `SESSION_EXPIRED`, `REQUEST_ENTITY_TOO_LARGE`, `RATE_LIMITED`, `INTERNAL_SERVER_ERROR`, `BAD_GATEWAY`, `SERVICE_UNAVAILABLE` (one response per status with its code as a constant). `FsmState` is `S0`, `S1`, `QUOTE_ONLY`, `S2`, `S3`, `HUMAN_ESCALATION`, `PAUSE`, `EXIT`, `EXIT_ADVISORY`, `DATA_ERASURE`, `HANDOFF`; `Locale` is `en-IN` or `hi-IN`; `DocumentKind` is `CIS`, `BI` or `POLICY_WORDING`.
