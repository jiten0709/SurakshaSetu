-- OD-1 (decided 2026-09-25, option a): a disclosure is keyed by (disclosure_id, language), so an
-- approved translation shares its ID; V3 keyed it by disclosure_id alone. Changed text needs a
-- new disclosure_id and a new registry_version, which the seed loader enforces: a body is never
-- edited in place, so the set hashes of older registry versions stay valid.
-- Table grants (V7) survive a key change, so none are repeated here.

ALTER TABLE catalog.disclosure DROP CONSTRAINT disclosure_pkey;
ALTER TABLE catalog.disclosure ADD PRIMARY KEY (disclosure_id, language);
