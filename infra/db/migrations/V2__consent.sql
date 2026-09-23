-- consent schema, written only by the Consent Service (domain_rw). Purposes: P1 (needs and
-- recommendation, required), P2 (advisor contact), P3 (marketing). The 18+ declaration is a
-- column on record, not a purpose.
-- TDD §7.2 DDL copied verbatim; columns marked "addition:" come from the implementation guide.

CREATE TABLE consent.notice_version (
  notice_version text PRIMARY KEY,                 -- 2026.09.1-en
  language       text NOT NULL,
  body           text NOT NULL,
  body_sha256    bytea NOT NULL,
  approved_by    text NOT NULL,
  effective_from date NOT NULL,
  is_dummy       boolean NOT NULL DEFAULT false    -- addition: fictitious seed text, for the release gate
);

CREATE TABLE consent.record (
  consent_id            uuid PRIMARY KEY,
  subject_ref           uuid NOT NULL,
  session_id            uuid NOT NULL,
  notice_version        text NOT NULL REFERENCES consent.notice_version,
  method                text NOT NULL CHECK (method IN ('structured_action','parsed_affirmation')),
  is_adult_declared     boolean NOT NULL,
  granted_at            timestamptz NOT NULL,
  withdrawn_at          timestamptz,               -- the only column domain_rw may UPDATE (I5)
  idempotency_key       text UNIQUE,               -- addition: dedupes a retried consent action
  notice_sha256         bytea NOT NULL,            -- addition: hash of the exact notice accepted
  ai_disclosure_version text NOT NULL,             -- addition: AI-disclosure copy shown with the notice
  language              text NOT NULL              -- addition: language the notice was rendered in
);

CREATE TABLE consent.purpose_grant (               -- history, not current state
  consent_id uuid NOT NULL REFERENCES consent.record,
  purpose    text NOT NULL CHECK (purpose IN ('P1','P2','P3')),
  granted    boolean NOT NULL,
  changed_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (consent_id, purpose, changed_at)
);
