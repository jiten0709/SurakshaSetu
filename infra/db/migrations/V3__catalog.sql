-- catalog schema: the Product Catalog and Disclosure Registry, loaded by catalog_loader. Product
-- numbers and mandatory text come only from here, never from the vector collections.
-- TDD §7.2 DDL copied verbatim; columns marked "addition:" and the four tables marked
-- "addition: whole table" come from the implementation guide.

CREATE TABLE catalog.product (
  uin              text PRIMARY KEY,               -- 999N001V02
  name             text NOT NULL,
  category         text NOT NULL,                  -- TERM, TERM_ROP, NON_PAR_SAVINGS, PAR, ULIP
  status           text NOT NULL,                  -- in_force, withdrawn
  entry_age_min    int NOT NULL,
  entry_age_max    int NOT NULL,
  maturity_age_max int NOT NULL,
  sa_min_inr       numeric(14,2) NOT NULL,
  sa_max_inr       numeric(14,2),
  term_years       int4range NOT NULL,
  ppt_options      text[] NOT NULL,                -- regular, limited_10, single
  payout_options   text[] NOT NULL,                -- lumpsum, monthly_income, lumpsum_plus_income
  rider_uins       text[] NOT NULL DEFAULT '{}',
  effective_from   date NOT NULL,
  effective_to     date,
  quote_defaults   jsonb NOT NULL DEFAULT '{}',    -- addition: default quote-engine inputs
  launch_enabled   boolean NOT NULL DEFAULT false, -- addition: seeded-but-off products, e.g. 999N010V01
  is_dummy         boolean NOT NULL DEFAULT false  -- addition: fictitious reference data
);

CREATE TABLE catalog.disclosure (
  disclosure_id  text PRIMARY KEY,                 -- DISC-GLOBAL-SOLICIT-01
  scope          text NOT NULL,                    -- GLOBAL, CATEGORY, UIN
  scope_key      text,                             -- category or UIN when scoped
  language       text NOT NULL,
  body           text NOT NULL,                    -- verbatim approved text
  body_sha256    bytea NOT NULL,
  approved_by    text NOT NULL,
  effective_from date NOT NULL,
  effective_to   date,
  is_dummy       boolean NOT NULL DEFAULT false    -- addition
);

CREATE TABLE catalog.disclosure_set (
  uin              text NOT NULL REFERENCES catalog.product,
  channel          text NOT NULL,
  language         text NOT NULL,
  registry_version text NOT NULL,
  disclosure_ids   text[] NOT NULL,                -- ordered
  set_sha256       bytea NOT NULL,                 -- over the complete, ordered set (I4)
  PRIMARY KEY (uin, channel, language, registry_version)
);

CREATE TABLE catalog.corpus_snapshot (
  snapshot_id text PRIMARY KEY,                    -- product-2026-09-01
  collection  text NOT NULL,                       -- regulatory, product, tax
  chunk_count int  NOT NULL,
  built_at    timestamptz NOT NULL,
  approved_by text NOT NULL,
  status      text NOT NULL,                       -- active, superseded
  meta        jsonb NOT NULL DEFAULT '{}'          -- addition: index statistics such as BM25 avgdl
);

-- addition: whole table. Riders carry their own UINs (999A007V01..); product.rider_uins lists them.
CREATE TABLE catalog.rider (
  uin         text PRIMARY KEY,
  name        text NOT NULL,
  attaches_to text[] NOT NULL,                     -- base product UINs
  sa_max_inr  numeric(14,2),
  is_dummy    boolean NOT NULL DEFAULT false
);

-- addition: whole table.
CREATE TABLE catalog.product_document (
  uin      text NOT NULL REFERENCES catalog.product,
  kind     text NOT NULL CHECK (kind IN ('CIS','BI','POLICY_WORDING')),
  version  text NOT NULL,
  language text NOT NULL,
  uri      text NOT NULL,
  sha256   bytea NOT NULL,
  is_dummy boolean NOT NULL DEFAULT false,
  PRIMARY KEY (uin, kind, version, language)
);

-- addition: whole table.
CREATE TABLE catalog.pincode (
  pincode     text PRIMARY KEY CHECK (pincode ~ '^[1-9][0-9]{5}$'),
  district    text NOT NULL,
  state       text NOT NULL,
  serviceable boolean NOT NULL,
  is_dummy    boolean NOT NULL DEFAULT false
);

-- addition: whole table.
CREATE TABLE catalog.occupation (
  code       text PRIMARY KEY,
  label      text NOT NULL,
  risk_class int NOT NULL CHECK (risk_class BETWEEN 1 AND 4),
  is_dummy   boolean NOT NULL DEFAULT false
);
