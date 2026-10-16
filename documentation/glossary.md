# Glossary

System terms used across the documentation.

| Term                       | Meaning                                                                                                                                |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| **ADB / WOP / CI**         | Rider families in the ranking rules: accidental death benefit, waiver of premium, critical illness                                     |
| **Anchor**                 | The daily Merkle root of every session's chain head, stored in an object-locked bucket with a time-stamp token                         |
| **Audit chain**            | A session's append-only, hash-linked sequence of audit events                                                                          |
| **Bundle (prompt bundle)** | A hash-locked directory of prompts, templates and lexicons that is pinned to a session                                                 |
| **CC / G rows**            | Cross-cutting (CC1–CC5b) and guard (G1–G4) rows of the transition table                                                                |
| **CIS / BI**               | Customer information sheet; benefit illustration. Two of the three document kinds acknowledged in S3 (the third is the policy wording) |
| **Consent purposes**       | P1 needs and recommendation (required), P2 advisor contact, P3 marketing; the 18+ declaration is separate                              |
| **DEK / KEK**              | Per-subject data-encryption key; the key-encryption key that wraps it                                                                  |
| **DMN**                    | Decision Model and Notation: the decision-table standard the rules are written in                                                      |
| **DPDP**                   | India's Digital Personal Data Protection Act 2023 (and the 2025 Rules)                                                                 |
| **DUMMY**                  | Placeholder content that a release gate must block from reaching a customer in pilot or production                                     |
| **Envelope**               | The two-message prompt (system + user) sent to a generation route after a clean PII scan                                               |
| **FSM**                    | Finite state machine; here a pure function over an explicit table of rows                                                              |
| **Golden conversation**    | A scripted YAML conversation replayed against the full stack with global assertions                                                    |
| **Hand-off**               | The signed intake payload posted to the application journey at S3 → S4                                                                 |
| **hi-Latn**                | Hindi written in Latin script ("Hinglish")                                                                                             |
| **I1–I8**                  | The eight invariants ([state-machine](state-machine.md#the-eight-invariants))                                                                                                     |
| **JCS**                    | RFC 8785 JSON Canonicalization Scheme, used for every hash                                                                             |
| **Kill switch**            | An audited, immediate withdrawal of a product, prompt bundle or route; the only exception to version pinning                           |
| **Lexicon pack**           | A versioned, compliance-owned list of output guardrail rules                                                                           |
| **OmniRoute**              | The AI gateway: the only egress for the chat models                                                                                    |
| **Pin**                    | A version fixed to a session when it starts (bundle, rules, corpus snapshot, consent notice)                                           |
| **PPT**                    | Premium payment term                                                                                                                   |
| **Quote-Only**             | The express path for a customer who names a plan; it cannot reach the hand-off                                                         |
| **RAGAS**                  | A set of RAG-evaluation metric definitions (recall, precision, faithfulness) the project follows                                       |
| **Rail**                   | A guardrail check on input, model output or the released turn                                                                          |
| **RRF**                    | Reciprocal rank fusion of the dense and sparse search results                                                                          |
| **Slot**                   | A named customer fact (age, tobacco use, income, …) with a status and confidence                                                       |
| **SA**                     | Sum assured (the cover amount)                                                                                                         |
| **Snapshot**               | An immutable, indexed edition of a KB collection, pinned per session                                                                   |
| **TDD**                    | The project's internal technical design document, the source of truth for stack, architecture, schemas and invariants                                     |
| **TEI**                    | Hugging Face text-embeddings-inference, serving the embedder and reranker                                                              |
| **UIN**                    | Unique identification number of an insurance product (fictitious `999N…` here)                                                         |
| **WORM**                   | Write once, read many: object-locked storage that cannot be altered or deleted before retention ends                                   |
