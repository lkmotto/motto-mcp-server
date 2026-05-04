-- Phase 5.6: tables backing the read/debug/replay tools (decisions, locks,
-- artifacts). Forward-only and idempotent — every CREATE uses IF NOT EXISTS.
--
-- Schema reconciliation note (Phase 5.6): the live Neon DB carries copies of
-- these tables in the public schema. db.py queries fleet.* (see 0001_init.sql),
-- which is what tests + local apply against. Production reconciliation between
-- public.* and fleet.* is handled out of band via the Neon console; this
-- migration only ensures the fleet.* shape that the new tools expect.

CREATE TABLE IF NOT EXISTS fleet.decisions (
    id          BIGSERIAL PRIMARY KEY,
    agent_id    INTEGER NOT NULL REFERENCES fleet.agents(id) ON DELETE CASCADE,
    run_id      UUID REFERENCES fleet.runs(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    choice      TEXT NOT NULL,
    rationale   TEXT,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_decisions_run_ts    ON fleet.decisions (run_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_agent_ts  ON fleet.decisions (agent_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_decisions_choice_ts ON fleet.decisions (choice, ts DESC);

CREATE TABLE IF NOT EXISTS fleet.locks (
    resource    TEXT PRIMARY KEY,
    holder_run  UUID REFERENCES fleet.runs(id) ON DELETE CASCADE,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_locks_expires_at ON fleet.locks (expires_at);

CREATE TABLE IF NOT EXISTS fleet.artifacts (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID REFERENCES fleet.runs(id) ON DELETE CASCADE,
    agent_id    INTEGER REFERENCES fleet.agents(id) ON DELETE CASCADE,
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind        TEXT NOT NULL,
    name        TEXT,
    content     JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run_ts ON fleet.artifacts (run_id, ts DESC);
