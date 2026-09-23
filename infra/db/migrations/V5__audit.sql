-- audit schema. Tables are owned by audit_owner (NOLOGIN), so no login role can ALTER, DROP or
-- TRUNCATE them; app_rw gets INSERT and SELECT only, granted centrally in V7.
-- hash = SHA-256(prev_hash || JCS(header) || SHA-256(payload_enc)), so it covers ciphertext and
-- non-personal headers, never plaintext. The genesis prev_hash is 32 zero bytes, seq is gap-free
-- per session, and event_id is a UUIDv7 supplied by the application.
-- TDD §4.3 DDL copied verbatim, qualified into the audit schema; chain_anchor is an addition.

CREATE TABLE audit.audit_event (
  event_id     uuid        PRIMARY KEY,   -- UUIDv7, time-ordered
  session_id   uuid        NOT NULL,
  seq          bigint      NOT NULL,      -- gap-free per session
  event_type   text        NOT NULL,
  occurred_at  timestamptz NOT NULL,      -- server clock, Indian NTP
  fsm_state    text        NOT NULL,
  pins         jsonb       NOT NULL,      -- prompt, rules, corpus, notice versions
  header       jsonb       NOT NULL,      -- non-personal, queryable fields
  payload_enc  bytea       NOT NULL,      -- AES-256-GCM under the subject key
  key_ref      text        NOT NULL,      -- subject key id in KMS
  prev_hash    bytea       NOT NULL,
  hash         bytea       NOT NULL,      -- SHA-256(prev_hash || header || SHA-256(payload_enc))
  UNIQUE (session_id, seq)
);

ALTER TABLE audit.audit_event OWNER TO audit_owner;     -- NOLOGIN role

-- addition: whole table. The nightly job's Merkle root, anchored in WORM with an RFC 3161 token.
CREATE TABLE audit.chain_anchor (
  anchor_date date PRIMARY KEY,
  merkle_root bytea NOT NULL,
  sessions    int NOT NULL,
  events      bigint NOT NULL,
  worm_key    text NOT NULL,
  tsa_token   bytea,
  created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE audit.chain_anchor OWNER TO audit_owner;
