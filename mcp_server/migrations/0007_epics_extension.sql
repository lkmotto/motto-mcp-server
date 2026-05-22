-- 0007_epics_extension.sql
--
-- Extends the public.epics table (owned by motto-director's migration
-- 0006_epics.sql) with GitHub-linked control-plane columns so that the
-- motto-mcp-server MCP tools (create_epic, epic_status, dispatch_droid,
-- pause_epic, kill_epic) and cockpit /cockpit/epics view can manage the
-- full lifecycle without touching motto-director tables directly.
--
-- IMPORTANT: public.epics is OWNED by motto-director. This file is
-- intentionally idempotent and additive only -- if the table does not
-- yet exist in this database, the whole migration is a no-op.
-- motto-director creates the table; motto-mcp-server layers in these
-- columns on its next boot.
--
-- New columns:
--   gh_issue_url          TEXT        -- URL of the GitHub Issue
--   gh_issue_number       INTEGER     -- GitHub issue number
--   factory_session_id    TEXT        -- Factory droid session locked to this epic
--   cost_so_far_usd       NUMERIC     -- accumulated droid spend
--   max_cost_usd          NUMERIC     -- budget ceiling
--   max_hours             INTEGER     -- wall-clock budget in hours
--   success_criteria_json JSONB       -- structured success criteria
--   last_progress_at      TIMESTAMPTZ -- last time a droid made observable progress

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'epics'
    ) THEN
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS gh_issue_url        TEXT NULL;
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS gh_issue_number     INTEGER NULL;
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS factory_session_id  TEXT NULL;
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS cost_so_far_usd     NUMERIC(10,4) DEFAULT 0;
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS max_cost_usd        NUMERIC(10,2) NULL;
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS max_hours           INTEGER NULL;
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS success_criteria_json JSONB NULL;
        ALTER TABLE public.epics ADD COLUMN IF NOT EXISTS last_progress_at    TIMESTAMPTZ NULL;

        CREATE INDEX IF NOT EXISTS epics_factory_session_id_idx
            ON public.epics (factory_session_id);
    END IF;
END
$$;

-- Backfill the Day-0 bootstrap epic (id=14) which was created before these
-- columns existed. Safe to run multiple times -- WHERE guards against overwrite.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM public.epics WHERE id = 14 AND gh_issue_url IS NULL
    ) THEN
        UPDATE public.epics SET
            gh_issue_url = 'https://github.com/lkmotto/motto-mcp-server/issues/64',
            gh_issue_number = 64,
            max_cost_usd = 25.00,
            max_hours = 8,
            status = 'active'
        WHERE id = 14;
    END IF;
END
$$;
