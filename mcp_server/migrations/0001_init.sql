-- Motto fleet-coordination schema. Applied by motto-mcp-server on startup.
-- Idempotent — every CREATE uses IF NOT EXISTS.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS fleet;

CREATE TABLE IF NOT EXISTS fleet.agents (
    id              SERIAL PRIMARY KEY,
    name            TEXT UNIQUE NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('variable', 'deterministic')),
    deploy_target   TEXT,
    version         TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb,
    registered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS fleet.runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    agent_id            INTEGER NOT NULL REFERENCES fleet.agents(id) ON DELETE CASCADE,
    parent_run_id       UUID REFERENCES fleet.runs(id) ON DELETE SET NULL,
    kind                TEXT NOT NULL,
    intent              TEXT,
    status              TEXT NOT NULL DEFAULT 'running'
                          CHECK (status IN ('running', 'success', 'error', 'cancelled')),
    langfuse_trace_id   TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at         TIMESTAMPTZ,
    summary             JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_runs_agent_started   ON fleet.runs (agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status_started  ON fleet.runs (status, started_at DESC);

CREATE TABLE IF NOT EXISTS fleet.events (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES fleet.agents(id) ON DELETE CASCADE,
    run_id      UUID REFERENCES fleet.runs(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    level       TEXT NOT NULL DEFAULT 'info'
                  CHECK (level IN ('debug', 'info', 'warn', 'error')),
    kind        TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_events_agent_ts  ON fleet.events (agent_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_events_run       ON fleet.events (run_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_kind_ts   ON fleet.events (kind, ts DESC);

CREATE TABLE IF NOT EXISTS fleet.intents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_agent_id INTEGER REFERENCES fleet.agents(id) ON DELETE CASCADE,
    source_agent_id INTEGER REFERENCES fleet.agents(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    status          TEXT NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'consumed', 'dismissed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    consumed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_intents_target_open
    ON fleet.intents (target_agent_id, status, created_at DESC);
