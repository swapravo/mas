-- Enterprise layer: department-scoped data. PostgreSQL 16 or 18 (no uuidv7, unlike ../schema.sql).
-- Run as a role that can create roles (a superuser):
--     psql -v ON_ERROR_STOP=1 -f enterprise/schema.sql <database>
-- Safe to run again: it drops and recreates only the ent_* objects.
--
-- One table, one rule: a department role sees and writes only the rows stamped with its own department.
-- Same pattern as ../schema.sql (deny by default, a role per actor, the owner bypasses RLS), keyed by department.

BEGIN;

DROP TABLE IF EXISTS ent_record, ent_audit CASCADE;
DROP FUNCTION IF EXISTS ent_dept();
DROP FUNCTION IF EXISTS ent_audit_immutable();

-- A role per department, plus the control plane (orchestrators, registry, approvals) and an owner that bypasses RLS.
-- Dev passwords only; a real deployment reads them from a secrets manager.
DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY['collections', 'ops', 'finance', 'legal', 'orch', 'owner'] LOOP
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ent_' || r) THEN
            EXECUTE format('CREATE ROLE %I LOGIN PASSWORD %L', 'ent_' || r, 'ent_' || r || '_pw');
        END IF;
    END LOOP;
END $$;

CREATE TABLE ent_record (
    dept    text  NOT NULL CHECK (dept IN ('collections', 'ops', 'finance', 'legal', 'orch')),
    kind    text  NOT NULL,                  -- dispute, repair, ledger, notice, waiver_applied, case_registry ...
    key     text  NOT NULL,
    payload jsonb NOT NULL,
    PRIMARY KEY (dept, kind, key)
);

-- 'ent_finance' -> 'finance'. The role you logged in as decides what you may see: nothing a session can SET.
CREATE FUNCTION ent_dept() RETURNS text LANGUAGE sql STABLE AS $$ SELECT substr(current_user, 5) $$;

ALTER TABLE ent_record ENABLE ROW LEVEL SECURITY;
CREATE POLICY dept_scope ON ent_record FOR ALL TO ent_collections, ent_ops, ent_finance, ent_legal, ent_orch
    USING (dept = ent_dept()) WITH CHECK (dept = ent_dept());

-- Everyone may SELECT and write the table. That is deliberate: it is the sloppy-grants case, so RLS is the layer
-- doing the work. (Revoke by table or column as well in a real system: defence in depth.)
GRANT SELECT, INSERT, UPDATE ON ent_record TO ent_collections, ent_ops, ent_finance, ent_legal, ent_orch;
GRANT EXECUTE ON FUNCTION ent_dept() TO ent_collections, ent_ops, ent_finance, ent_legal, ent_orch;
ALTER TABLE ent_record OWNER TO ent_owner;          -- the owner bypasses RLS: use only to seed and to check

CREATE TABLE ent_audit (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    actor   text NOT NULL,
    action  text NOT NULL,
    target  text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'
);
GRANT INSERT ON ent_audit TO ent_collections, ent_ops, ent_finance, ent_legal, ent_orch;
ALTER TABLE ent_audit OWNER TO ent_owner;

-- Insert only, even against the owner (same triggers as ../schema.sql).
CREATE FUNCTION ent_audit_immutable() RETURNS trigger LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'ent_audit is insert only'; END $$;
CREATE TRIGGER ent_audit_no_change BEFORE UPDATE OR DELETE ON ent_audit
    FOR EACH ROW EXECUTE FUNCTION ent_audit_immutable();
CREATE TRIGGER ent_audit_no_truncate BEFORE TRUNCATE ON ent_audit
    FOR EACH STATEMENT EXECUTE FUNCTION ent_audit_immutable();

GRANT USAGE ON SCHEMA public TO ent_collections, ent_ops, ent_finance, ent_legal, ent_orch, ent_owner;

COMMIT;
