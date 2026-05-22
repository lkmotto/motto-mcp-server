-- 0006_pending_moves_claimed_status.sql
--
-- Add droid-claim bookkeeping to public.pending_moves so droid runners can
-- atomically pull approved moves off the queue (see the claim_next_step /
-- release_claimed_step MCP tools added alongside this migration).
--
-- IMPORTANT: public.pending_moves is OWNED by motto-director's migrations
-- (motto-director/migrations/0005_pending_moves.sql). This file is
-- intentionally idempotent and additive only — if the table does not yet
-- exist in this database, the whole migration is a no-op. motto-director
-- will create the table; motto-mcp-server will run this on the next boot
-- after that to layer in the claim columns.
--
-- Status values used by claim_next_step:
--   pending  → human review pending
--   approved → human said yes, ready for a droid to claim
--   claimed  → a droid (claimed_by) has locked this row
--   applied  → droid finished the move
--   rejected → human said no
--
-- The status column is plain TEXT with no check constraint at the time
-- of writing, so adding a new value 'claimed' requires no DDL. If a
-- future migration adds a CHECK or ENUM, that migration MUST include
-- 'claimed' in the allowed set. See TODO at bottom.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema = 'public'
          AND table_name = 'pending_moves'
    ) THEN
        ALTER TABLE public.pending_moves
            ADD COLUMN IF NOT EXISTS claimed_by TEXT NULL;
        ALTER TABLE public.pending_moves
            ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ NULL;

        CREATE INDEX IF NOT EXISTS idx_pending_moves_status_priority_created
            ON public.pending_moves (status, priority DESC, created_at ASC);
    END IF;
END
$$;

-- TODO(human): if motto-director ever adds a CHECK constraint or enum
-- type on public.pending_moves.status, ensure 'claimed' is included as
-- a permitted value. This migration is reversible by dropping the
-- claimed_by/claimed_at columns; no destructive ALTERs here.
