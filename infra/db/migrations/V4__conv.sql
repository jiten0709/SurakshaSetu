-- conv schema: the conversation tier's own records. The checkpointer's state lives in langgraph.
-- TDD §7.2 DDL copied verbatim; columns marked "addition:" and kill_switch come from the
-- implementation guide.

CREATE TABLE conv.session (
  session_id       uuid PRIMARY KEY,
  subject_ref      uuid NOT NULL,                  -- pseudonymous customer id
  key_ref          text NOT NULL,                  -- subject DEK in KMS
  channel          text NOT NULL,                  -- web, app
  locale           text NOT NULL DEFAULT 'en-IN',
  fsm_state        text NOT NULL,                  -- S0..S3, QUOTE_ONLY, PAUSE, HANDOFF, ...
  frame_stack      jsonb NOT NULL DEFAULT '[]',    -- side-query frames (2.6)
  pins             jsonb NOT NULL,                 -- prompt, rules, corpus, notice versions (I7)
  status           text NOT NULL DEFAULT 'active', -- active, paused, ended, erased
  created_at       timestamptz NOT NULL DEFAULT now(),
  last_activity_at timestamptz NOT NULL DEFAULT now(),
  expires_at       timestamptz NOT NULL,           -- session TTL (4.4)
  token_sha256     bytea UNIQUE NOT NULL,          -- addition: the session token's hash, never the token
  consent_id       uuid REFERENCES consent.record, -- addition: set once P1 consent exists
  counters         jsonb NOT NULL DEFAULT '{}'     -- addition: invalid-input and injection-hit counts
);

CREATE TABLE conv.turn (
  turn_id     uuid PRIMARY KEY,
  session_id  uuid NOT NULL REFERENCES conv.session,
  seq         int  NOT NULL,
  direction   text NOT NULL CHECK (direction IN ('in','out')),
  text_enc    bytea NOT NULL,                      -- AES-256-GCM under the subject DEK
  redacted    text  NOT NULL,                      -- token-substituted, safe to search
  language    text  NOT NULL,
  analysis    jsonb,                               -- TurnAnalysis (3.3)
  created_at  timestamptz NOT NULL DEFAULT now(),
  turn_key    uuid NOT NULL,                       -- addition: the turn's idempotency key
  UNIQUE (session_id, seq),
  UNIQUE (session_id, turn_key)                    -- addition
);

CREATE TABLE conv.slot_value (                     -- append-only, never updated
  slot_value_id bigserial PRIMARY KEY,
  session_id    uuid NOT NULL REFERENCES conv.session,
  slot          text NOT NULL,                     -- age, tobacco_12m, annual_income_inr, ...
  value_enc     bytea NOT NULL,
  confidence    numeric(4,3) NOT NULL,
  status        text NOT NULL CHECK (status IN ('proposed','confirmed','corrected','declined')),
  source_turn   uuid REFERENCES conv.turn,
  evidence      text,                              -- span read back to the customer
  created_at    timestamptz NOT NULL DEFAULT now(),
  consent_id    uuid NOT NULL REFERENCES consent.record  -- addition: I1 backstop, no slot without consent
);
CREATE INDEX ON conv.slot_value (session_id, slot, created_at DESC);

CREATE TABLE conv.recommendation (
  rec_id                    uuid PRIMARY KEY,
  session_id                uuid NOT NULL REFERENCES conv.session,
  options                   jsonb NOT NULL,        -- RecommendationPayload.options (3.8)
  ranker_version            text NOT NULL,
  suitability_inputs_sha256 bytea NOT NULL,        -- equals the confirmed slot hash (I2)
  rendered_sha256           bytea NOT NULL,
  created_at                timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE conv.disclosure_ack (
  ack_id           uuid PRIMARY KEY,
  rec_id           uuid NOT NULL REFERENCES conv.recommendation,
  uin              text NOT NULL,
  registry_version text NOT NULL,
  set_sha256       bytea NOT NULL,
  document_sha256  jsonb NOT NULL,                 -- CIS, illustration, wording -> hash shown
  acked_at         timestamptz NOT NULL
);

CREATE TABLE conv.handoff (
  handoff_id  uuid PRIMARY KEY,
  session_id  uuid NOT NULL REFERENCES conv.session,
  reason_code text NOT NULL,                       -- HE_REQUEST, HE_NRI, HE_VULNERABLE, ...
  queue       text NOT NULL,
  payload_enc bytea NOT NULL,                      -- advisor briefing or intake payload
  created_at  timestamptz NOT NULL DEFAULT now(),
  picked_at   timestamptz
);

-- addition: whole table.
CREATE TABLE conv.kill_switch (
  id         uuid PRIMARY KEY,
  kind       text NOT NULL CHECK (kind IN ('product','prompt_bundle','route')),
  target     text NOT NULL,
  active     boolean NOT NULL,
  reason     text NOT NULL,
  actor      text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
