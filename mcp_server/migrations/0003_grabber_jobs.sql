-- Migration 0003: grabber job queue and playbook whitelist
-- Applied by mcp_server.db.Database.apply_migrations() at startup.

create table if not exists fleet.grabber_jobs (
  id           uuid primary key default gen_random_uuid(),
  service      text not null,
  reason       text not null,
  requested_by text not null,
  status       text not null check (status in ('pending','running','succeeded','failed','cancelled')) default 'pending',
  created_at   timestamptz not null default now(),
  started_at   timestamptz,
  ended_at     timestamptz,
  error_class  text,
  audit_decision_id uuid references fleet.decisions(id) on delete set null
);
create index if not exists grabber_jobs_status_created on fleet.grabber_jobs(status, created_at desc);

create table if not exists fleet.grabber_playbooks (
  service              text primary key,
  dashboard_url        text not null,
  target_doppler_keys  text[] not null,
  last_validated_at    timestamptz,
  status               text not null check (status in ('live','placeholder','disabled')) default 'placeholder'
);

-- Seed the anthropic playbook in placeholder state
insert into fleet.grabber_playbooks (service, dashboard_url, target_doppler_keys, status)
values ('anthropic', 'https://console.anthropic.com', array['ANTHROPIC_API_KEY','CLAUDE_CODE_OAUTH_TOKEN'], 'placeholder')
on conflict (service) do nothing;
