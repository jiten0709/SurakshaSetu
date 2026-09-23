-- Roles and schemas. Flyway runs this as the postgres superuser once per database
-- (surakshasetu, then surakshasetu_test). Its own history table lives in schema `flyway`,
-- which Flyway creates; no other role has any privilege there.

-- Roles are cluster-wide, so the second database's run finds them already created.
DO $$
DECLARE
    r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['audit_owner', 'app_rw', 'domain_rw', 'catalog_loader', 'erasure_rw',
                             'keyvault_rw', 'compliance_ro'] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END
$$;

-- audit_owner stays NOLOGIN: it only owns the audit tables (V5). Passwords are Flyway
-- placeholders fed from infra/.env, never literals.
ALTER ROLE app_rw LOGIN PASSWORD '${app_rw_password}';
ALTER ROLE domain_rw LOGIN PASSWORD '${domain_rw_password}';
ALTER ROLE catalog_loader LOGIN PASSWORD '${catalog_loader_password}';
ALTER ROLE erasure_rw LOGIN PASSWORD '${erasure_rw_password}';
ALTER ROLE keyvault_rw LOGIN PASSWORD '${keyvault_rw_password}';
ALTER ROLE compliance_ro LOGIN PASSWORD '${compliance_ro_password}';

CREATE SCHEMA conv;
CREATE SCHEMA consent;
CREATE SCHEMA catalog;
CREATE SCHEMA audit;
CREATE SCHEMA keyvault;
-- Empty here: PostgresSaver.setup(), connected as app_rw, creates the checkpointer's tables.
CREATE SCHEMA langgraph;
