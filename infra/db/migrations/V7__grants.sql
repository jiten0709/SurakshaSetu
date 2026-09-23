-- Privilege model. Least privilege per role; PUBLIC gets nothing in any schema. There are no
-- default privileges on these schemas, so every later migration must grant on its new tables.

REVOKE ALL ON SCHEMA public, conv, consent, catalog, audit, keyvault, langgraph FROM PUBLIC;

-- app_rw: conv ALL except slot_value (INSERT, SELECT only); audit INSERT, SELECT; consent SELECT;
-- catalog SELECT; langgraph ALL; sequences USAGE.
GRANT USAGE ON SCHEMA conv, audit, consent, catalog TO app_rw;
GRANT ALL ON ALL TABLES IN SCHEMA conv TO app_rw;
REVOKE ALL ON conv.slot_value FROM app_rw;
GRANT INSERT, SELECT ON conv.slot_value TO app_rw; -- slot history is append-only
GRANT INSERT, SELECT ON ALL TABLES IN SCHEMA audit TO app_rw; -- the audit chain is append-only
GRANT SELECT ON ALL TABLES IN SCHEMA consent, catalog TO app_rw;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA conv, audit TO app_rw;
-- Includes CREATE: PostgresSaver.setup() runs as app_rw, which then owns the tables it makes.
GRANT ALL ON SCHEMA langgraph TO app_rw;

-- domain_rw (Consent Service): the only writer of consent. A record is immutable except for
-- its withdrawal timestamp.
GRANT USAGE ON SCHEMA consent, catalog TO domain_rw;
GRANT INSERT, SELECT ON consent.record TO domain_rw;
GRANT UPDATE (withdrawn_at) ON consent.record TO domain_rw;
GRANT INSERT, SELECT ON consent.purpose_grant, consent.notice_version TO domain_rw;
GRANT SELECT ON ALL TABLES IN SCHEMA catalog TO domain_rw;

-- catalog_loader: loads the Product Catalog and Disclosure Registry.
GRANT USAGE ON SCHEMA catalog TO catalog_loader;
GRANT ALL ON ALL TABLES IN SCHEMA catalog TO catalog_loader;

-- erasure_rw: DELETE, SELECT on conv.* and langgraph.*. The langgraph tables don't exist until
-- app_rw creates them, so that half is a default privilege on app_rw's future tables.
GRANT USAGE ON SCHEMA conv, langgraph TO erasure_rw;
GRANT DELETE, SELECT ON ALL TABLES IN SCHEMA conv TO erasure_rw;
ALTER DEFAULT PRIVILEGES FOR ROLE app_rw IN SCHEMA langgraph GRANT DELETE, SELECT ON TABLES TO erasure_rw;

-- keyvault_rw: the only role that touches wrapped DEKs.
GRANT USAGE ON SCHEMA keyvault TO keyvault_rw;
GRANT ALL ON ALL TABLES IN SCHEMA keyvault TO keyvault_rw;

-- compliance_ro: reads audit and consent evidence; writes nothing.
GRANT USAGE ON SCHEMA audit, consent TO compliance_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA audit, consent TO compliance_ro;
