-- Verify_move framework (May 2026)
--
-- Three concerns owned by motto-mcp-server (the cockpit's home):
--   1. move_verifications  — outcome of every verifier run, keyed to a move
--   2. capability_requests — director asks for a connector/secret/credential,
--                            human grants in cockpit before director proceeds
--   3. trust_scores        — per-repo + global rolling trust score updated on
--                            verify.passed / verify.failed events
--
-- Verifier dispatch lives in mcp_server/verifiers/. The verify_move MCP tool
-- looks up the move, calls the right verifier, and writes the result row.
--
-- Tables live in fleet.* schema (same as events, runs, agents) so the
-- existing observability helpers can join freely.

CREATE TABLE IF NOT EXISTS fleet.move_verifications (
    id              SERIAL PRIMARY KEY,
    move_id         INTEGER NOT NULL,        -- pending_moves.id (public schema)
    repo            TEXT NOT NULL,
    kind            TEXT NOT NULL,           -- noop / merge_pr / file_issue / spawn_session / compound_pr / ...
    verifier        TEXT NOT NULL,           -- which verifier ran (e.g. "noop", "merge_pr.ci_green", "sdr.dry_run")
    status          TEXT NOT NULL,           -- passed | failed | inconclusive | error
    evidence        JSONB NOT NULL DEFAULT '{}'::jsonb,
    kpi_delta       JSONB NOT NULL DEFAULT '{}'::jsonb,
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    duration_ms     INTEGER,
    requested_by    TEXT,                    -- "director:<run_id>" | "cockpit:<token-prefix>"
    UNIQUE (move_id, verifier, started_at)
);

CREATE INDEX IF NOT EXISTS idx_move_verifications_move
    ON fleet.move_verifications (move_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_move_verifications_repo_status
    ON fleet.move_verifications (repo, status, started_at DESC);


CREATE TABLE IF NOT EXISTS fleet.capability_requests (
    id              SERIAL PRIMARY KEY,
    capability      TEXT NOT NULL,           -- e.g. "postmark_api_key" | "pcloud_write_token" | "linkedin_ads_oauth"
    repo            TEXT,                    -- where the verifier needs it (nullable for cross-repo)
    move_id         INTEGER,                 -- the move whose verifier needed it (nullable for proactive)
    justification   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending | granted | denied | satisfied
    requested_by    TEXT NOT NULL,           -- "director:<run_id>"
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decided_by      TEXT,                    -- "cockpit:<token-prefix>"
    decided_at      TIMESTAMPTZ,
    grant_detail    TEXT,                    -- secret-name, env-var, or note about how it was granted
    deny_reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_capability_requests_status
    ON fleet.capability_requests (status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_capability_requests_capability
    ON fleet.capability_requests (capability, status);


CREATE TABLE IF NOT EXISTS fleet.trust_scores (
    id              SERIAL PRIMARY KEY,
    scope           TEXT NOT NULL,           -- "global" | repo URL like "lkmotto/motto-sdr-agent"
    score           REAL NOT NULL,           -- rolling 0.0–1.0
    sample_size     INTEGER NOT NULL DEFAULT 0,
    last_passed_at  TIMESTAMPTZ,
    last_failed_at  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (scope)
);

-- Seed global + per-repo to 0.5 so the first verify event moves the needle
-- visibly. Real repos auto-insert on first verify.
INSERT INTO fleet.trust_scores (scope, score)
VALUES ('global', 0.5)
ON CONFLICT (scope) DO NOTHING;
