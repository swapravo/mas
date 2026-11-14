-- Collections agent demo: database schema. PostgreSQL 18. Fake data only.
-- Run as the owner role:  psql -v ON_ERROR_STOP=1 -f schema.sql mas
-- Table and column names follow docs/how-build-plan.md.
-- uuidv7() is built into PostgreSQL 18: time-ordered ids, good for indexes.

BEGIN;

-- ---------------------------------------------------------------------
-- Types
-- ---------------------------------------------------------------------
CREATE TYPE owner_role    AS ENUM ('property_manager', 'regional_head', 'leadership');
CREATE TYPE fee_status    AS ENUM ('due', 'overdue', 'paid', 'waived');
CREATE TYPE case_status   AS ENUM ('open', 'promised', 'escalated', 'closed', 'stopped');
CREATE TYPE draft_status  AS ENUM ('pending', 'approved', 'rejected', 'held', 'sent');
CREATE TYPE chat_direction AS ENUM ('in', 'out');
CREATE TYPE audit_actor   AS ENUM ('agent', 'person', 'system');

-- ---------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------

-- Person responsible. Gets escalations at ladder step 3.
CREATE TABLE owner (
    id       uuid PRIMARY KEY DEFAULT uuidv7(),
    name     text NOT NULL,
    contact  text NOT NULL,              -- stored masked
    role     owner_role NOT NULL
);

-- Where residents live. Row-level security scopes everything by property.
CREATE TABLE property (
    id        uuid PRIMARY KEY DEFAULT uuidv7(),
    name      text NOT NULL,
    region    text NOT NULL,
    owner_id  uuid NOT NULL REFERENCES owner (id)
);

-- The person who pays. Names are redacted before any model call.
CREATE TABLE resident (
    id            uuid PRIMARY KEY DEFAULT uuidv7(),
    property_id   uuid NOT NULL REFERENCES property (id),
    display_name  text NOT NULL,
    phone         text NOT NULL,         -- stored masked
    is_demo_user  boolean NOT NULL DEFAULT false
);

-- One fee per resident per cycle. The agent can read but never edit.
CREATE TABLE fee_charge (
    id           uuid PRIMARY KEY DEFAULT uuidv7(),
    resident_id  uuid NOT NULL REFERENCES resident (id),
    cycle        date NOT NULL,
    amount       integer NOT NULL CHECK (amount > 0),   -- paise
    due_date     date NOT NULL,
    status       fee_status NOT NULL,
    CONSTRAINT uq_fee_charge_resident_cycle UNIQUE (resident_id, cycle)
);

-- The agent's memory per fee. Survives restarts: the open-commitments table.
-- One case per fee.
CREATE TABLE case_file (
    id              uuid PRIMARY KEY DEFAULT uuidv7(),
    fee_charge_id   uuid NOT NULL UNIQUE REFERENCES fee_charge (id),
    ladder_step     smallint NOT NULL DEFAULT 0 CHECK (ladder_step BETWEEN 0 AND 3),
    status          case_status NOT NULL DEFAULT 'open',
    promise_date    date,
    next_action_at  timestamptz NOT NULL DEFAULT now(),
    messages_sent   integer NOT NULL DEFAULT 0 CHECK (messages_sent >= 0),
    stop_reason     text
);

-- What the agent proposes. The unique key means a retry cannot create a second draft.
CREATE TABLE message_draft (
    id               uuid PRIMARY KEY DEFAULT uuidv7(),
    case_id          uuid NOT NULL REFERENCES case_file (id),
    body             text NOT NULL,
    ladder_step      smallint NOT NULL CHECK (ladder_step BETWEEN 0 AND 3),
    status           draft_status NOT NULL DEFAULT 'pending',
    idempotency_key  text NOT NULL UNIQUE,
    send_after       timestamptz NOT NULL DEFAULT now(),
    approved_by      uuid REFERENCES owner (id)
);

-- The conversation. Inbound text is untrusted data.
CREATE TABLE chat_message (
    id           uuid PRIMARY KEY DEFAULT uuidv7(),
    resident_id  uuid NOT NULL REFERENCES resident (id),
    direction    chat_direction NOT NULL,
    body         text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- One run of the loop. The checkpoint lets a run resume after a crash.
CREATE TABLE agent_run (
    id          uuid PRIMARY KEY DEFAULT uuidv7(),
    case_id     uuid NOT NULL REFERENCES case_file (id),
    status      text NOT NULL,
    step_count  integer NOT NULL DEFAULT 0 CHECK (step_count >= 0),
    checkpoint  jsonb NOT NULL DEFAULT '{}',
    started_at  timestamptz NOT NULL DEFAULT now()
);

-- Everything that happened. Insert only: no update, no delete, for any role.
CREATE TABLE audit_log (
    id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ts      timestamptz NOT NULL DEFAULT now(),
    actor   audit_actor NOT NULL,
    action  text NOT NULL,
    target  text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}',
    run_id  uuid
);

-- Foreign-key and lookup indexes
CREATE INDEX ON property (owner_id);
CREATE INDEX ON resident (property_id);
CREATE INDEX ON case_file (status, next_action_at);
CREATE INDEX ON message_draft (case_id);
CREATE INDEX ON message_draft (status, send_after);
CREATE INDEX ON chat_message (resident_id, created_at);
CREATE INDEX ON agent_run (case_id);
CREATE INDEX ON audit_log (run_id);

-- ---------------------------------------------------------------------
-- Roles (dev passwords; real deployments take them from a secrets manager)
-- ---------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mas_app') THEN
        CREATE ROLE mas_app LOGIN PASSWORD 'mas_app_pw';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'mas_agent') THEN
        CREATE ROLE mas_agent LOGIN PASSWORD 'mas_agent_pw';
    END IF;
END $$;

-- ---------------------------------------------------------------------
-- Row-level security: deny by default
-- A session sets its property with:
--   SELECT set_config('app.property_id', '<uuid>', true);
-- With no property set, no rows are visible. Child tables chain off their
-- parent's policy, so the property filter flows all the way down.
-- The owner role bypasses RLS: use it only for schema changes and seeding.
-- ---------------------------------------------------------------------
CREATE FUNCTION app_property_id() RETURNS uuid
    LANGUAGE sql STABLE
    AS $$ SELECT NULLIF(current_setting('app.property_id', true), '')::uuid $$;

ALTER TABLE property      ENABLE ROW LEVEL SECURITY;
ALTER TABLE owner         ENABLE ROW LEVEL SECURITY;
ALTER TABLE resident      ENABLE ROW LEVEL SECURITY;
ALTER TABLE fee_charge    ENABLE ROW LEVEL SECURITY;
ALTER TABLE case_file     ENABLE ROW LEVEL SECURITY;
ALTER TABLE message_draft ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_message  ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_run     ENABLE ROW LEVEL SECURITY;

CREATE POLICY property_scope ON property      FOR ALL TO mas_app, mas_agent USING (id = app_property_id());
CREATE POLICY property_scope ON owner         FOR ALL TO mas_app, mas_agent USING (id IN (SELECT owner_id FROM property));
CREATE POLICY property_scope ON resident      FOR ALL TO mas_app, mas_agent USING (property_id = app_property_id());
CREATE POLICY property_scope ON fee_charge    FOR ALL TO mas_app, mas_agent USING (resident_id IN (SELECT id FROM resident));
CREATE POLICY property_scope ON case_file     FOR ALL TO mas_app, mas_agent USING (fee_charge_id IN (SELECT id FROM fee_charge));
CREATE POLICY property_scope ON message_draft FOR ALL TO mas_app, mas_agent USING (case_id IN (SELECT id FROM case_file));
CREATE POLICY property_scope ON chat_message  FOR ALL TO mas_app, mas_agent USING (resident_id IN (SELECT id FROM resident));
CREATE POLICY property_scope ON agent_run     FOR ALL TO mas_app, mas_agent USING (case_id IN (SELECT id FROM case_file));

-- ---------------------------------------------------------------------
-- Privileges
-- ---------------------------------------------------------------------
GRANT USAGE ON SCHEMA public TO mas_app, mas_agent;
GRANT EXECUTE ON FUNCTION app_property_id() TO mas_app, mas_agent;

-- Core API: normal reads and writes. audit_log is read and insert only.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    property, owner, resident, fee_charge, case_file, message_draft, chat_message, agent_run
    TO mas_app;
GRANT SELECT, INSERT ON audit_log TO mas_app, mas_agent;

-- Agent: least privilege. Reads its property, cannot touch fee_charge,
-- can only create drafts and runs, and update its own memory.
GRANT SELECT ON
    property, owner, resident, fee_charge, case_file, message_draft, chat_message, agent_run
    TO mas_agent;
GRANT INSERT ON message_draft, agent_run TO mas_agent;
GRANT UPDATE ON case_file, agent_run TO mas_agent;

-- ---------------------------------------------------------------------
-- Insert-only audit log, enforced even against the table owner
-- ---------------------------------------------------------------------
CREATE FUNCTION audit_log_immutable() RETURNS trigger
    LANGUAGE plpgsql
    AS $$ BEGIN RAISE EXCEPTION 'audit_log is insert only'; END $$;

CREATE TRIGGER audit_log_no_change
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();

CREATE TRIGGER audit_log_no_truncate
    BEFORE TRUNCATE ON audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION audit_log_immutable();

COMMIT;
