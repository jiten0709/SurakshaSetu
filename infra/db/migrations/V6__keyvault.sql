-- keyvault schema: per-subject DEKs, wrapped by an India-region KMS key. Erasure is
-- crypto-shredding: wrapped_dek is nulled and destroyed_at set, so every ciphertext under the key
-- becomes unreadable while the rows that reference it stay intact.
-- Whole schema is an addition: the TDD's five schemas do not include it.

CREATE TABLE keyvault.subject_key (
  key_ref       text PRIMARY KEY,
  subject_ref   uuid NOT NULL,
  wrapped_dek   bytea,                             -- null once destroyed (crypto-shredded)
  kek_id        text NOT NULL,                     -- India-region KMS key that wrapped this DEK
  created_at    timestamptz NOT NULL DEFAULT now(),
  destroy_after timestamptz,
  destroyed_at  timestamptz
);
