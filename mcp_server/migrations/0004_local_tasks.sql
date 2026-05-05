-- Migration 0004: local-task queue for the motto-local laptop bridge.
-- A cloud-side queue of work that the user's local agent claims and executes.
-- Use cases: file ops, screenshot/OCR, spawn local Claude Code, persistent
-- browser sessions, voice-memo transcription, anything that benefits from
-- running on the user's machine vs. a sandbox.

create table if not exists fleet.local_tasks (
  id           uuid primary key default gen_random_uuid(),
  -- What kind of task: 'shell', 'read_file', 'write_file', 'screenshot',
  -- 'claude_code', 'ocr', 'browser', 'echo', etc. Runner decides what it
  -- supports; unknown kinds get rejected.
  kind         text not null,
  -- Free-form payload — runner-side schema. e.g. for 'shell':
  -- {"command":"ls -la","cwd":"~","timeout_s":30}
  payload      jsonb not null default '{}'::jsonb,
  -- Origin: who queued this. 'cockpit-user', 'motto-director', etc.
  source       text not null default 'cockpit-user',
  -- Lifecycle
  status       text not null check (status in
                  ('queued','claimed','running','succeeded','failed','cancelled','expired'))
                default 'queued',
  -- Optional human-readable note shown in cockpit
  description  text,
  -- For idempotency / dedup; agents can pass a stable id to avoid re-queueing
  dedup_key    text,
  -- When the runner claimed it (set at claim_local_tasks)
  claimed_at   timestamptz,
  claimed_by   text,
  started_at   timestamptz,
  finished_at  timestamptz,
  -- Result of the task — runner-defined shape
  result       jsonb,
  -- Error class + message if status='failed'
  error        text,
  -- Maximum lifetime in seconds before the queue auto-expires un-claimed work
  ttl_seconds  integer not null default 600,
  created_at   timestamptz not null default now()
);

create index if not exists local_tasks_status_created
  on fleet.local_tasks(status, created_at desc);
create index if not exists local_tasks_dedup
  on fleet.local_tasks(dedup_key)
  where dedup_key is not null;

-- Helper: expire ancient queued/claimed tasks. Idempotent. Called periodically
-- from claim_local_tasks so we don't need a separate sweeper.
create or replace function fleet.expire_local_tasks()
returns void language sql as $$
  update fleet.local_tasks
     set status = 'expired',
         finished_at = now(),
         error = 'ttl exceeded'
   where status in ('queued','claimed','running')
     and created_at < now() - (ttl_seconds || ' seconds')::interval;
$$;
