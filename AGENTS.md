# AGENTS.md

> Operational spec for autonomous coding agents (Factory Droid, Codex, Cursor, Aider). Human-readable too.

## Identity
- **Repo:** `lkmotto/motto-mcp-server`
- **Purpose:** FastMCP fleet-coordination server backed by Neon Postgres; the central nervous system for variable agents to register, heartbeat, emit events, and pass cross-agent intent signals.
- **Status:** Active on Northflank — last commit 2026-05-09
- **Owner:** Luke Motto (`ljm32901@gmail.com`)
- **Linear team:** Mottoappraisal (MOT) · project Fleet Operations

## What this code does
HTTP + MCP server that provides fleet coordination tools for variable agents (`motto-director`, `motto-sdr-agent`, `motto-social-agent`). Agents register themselves, send heartbeats, open/close runs, emit events, and post cross-agent intent signals. `motto-director` reads fleet state via `get_fleet_status` and `get_recent_events` for its perceive step. Also serves an HTML dashboard and JSON status endpoint. Neon Postgres schema applied automatically on boot.

## Architecture at a glance
- `mcp_server/server.py` — FastMCP server; HTTP surface (`/mcp/`, `/dashboard`, `/cockpit`, `/fleet/status.json`, `/healthz`). Mounts every domain sub-server when `MOTTO_MCP_MOUNT_DOMAIN_SERVERS=1`.
- `mcp_server/db.py` — Neon Postgres connection + query helpers
- `mcp_server/tools_fleet_proxy.py` — Fleet proxy tools (pipeline + cockpit via private Northflank DNS)
- `mcp_server/cockpit.py` — Interactive cockpit UI (chat + intent submit)
- `mcp_server/chat_tools.py` — Director chat integration
- `mcp_server/migrations/` — Idempotent SQL migrations applied on startup
- `servers/grabber/` — grabber MCP server (credential rotation job queue, 6 tools, namespace `grabber`)
- `servers/cloudflare/` — Cloudflare API: accounts/zones/DNS/Workers/Pages/KV/R2 (15 tools, namespace `cloudflare`)
- `servers/northflank/` — Northflank API: projects/services/jobs/secret-groups (13 tools, namespace `northflank`)
- `servers/github/` — GitHub REST API: repos/issues/pulls + Actions secret provisioning (9 tools, namespace `github`)
- `servers/linear/` — Linear GraphQL API: issues/projects/comments (6 tools, namespace `linear`)
- `servers/apollo/` — Apollo.io API: people search/enrich + sequence ops (4 tools, namespace `apollo`)
- `servers/supabase/` — Supabase read-only SQL passthrough — SELECT only, refused at the tool boundary (3 tools, namespace `supabase`)
- `servers/doppler/` — Doppler secrets: allowlist-gated `read_secret`/`list_secret_names` plus audit/rename surface (10 tools, namespace `doppler`)
- `pyproject.toml` — Package; entry point: `motto-mcp-server`. Per-domain entry points: `motto-{cloudflare,northflank,github,linear,apollo,supabase,doppler,grabber}-mcp`.
- `Dockerfile` — Container image for Northflank

### Mounted tool surface (Factory MCP bridge)
With `MOTTO_MCP_MOUNT_DOMAIN_SERVERS=1` the main server exposes the following namespaces. Each tool is invoked as `<namespace>__<tool>`:

| Namespace | Tool count | Tools |
|---|---|---|
| `grabber` | 6 | (see `servers/grabber/SKILL.md`) |
| `cloudflare` | 15 | `list_accounts`, `list_zones`, `get_zone`, `list_dns_records`, `list_workers`, `get_worker`, `list_pages_projects`, `get_pages_project`, `list_pages_deployments`, `list_kv_namespaces`, `list_r2_buckets`, `create_dns_record`*, `delete_dns_record`*, `purge_zone_cache`*, `redeploy_pages`* |
| `northflank` | 13 | `list_projects`, `get_project`, `list_services`, `get_service`, `list_jobs`, `get_job`, `list_secret_groups`, `get_secret_group`, `get_recent_logs`, `restart_service`*, `redeploy_service`*, `trigger_job_run`*, `resync_secret_group`* |
| `github` | 9 | `list_repos`, `get_repo`, `list_issues`, `list_pulls`, `create_issue`*, `comment_issue`*, `create_pull`*, `merge_pull`*, `set_secret`* |
| `linear` | 6 | `list_issues`, `get_issue`, `list_projects`, `create_issue`*, `update_issue`*, `create_comment`* |
| `apollo` | 4 | `search_people`, `enrich_person`, `list_sequences`, `add_to_sequence`* |
| `supabase` | 3 | `query` (SELECT-only), `list_tables`, `describe_table` |
| `doppler` | 10 | `doppler_projects_list`, `doppler_configs_list`, `doppler_secrets_list`, `doppler_secret_get`, `list_secret_names`, `read_secret` (allowlist-gated), `doppler_secret_set`*, `doppler_secret_delete`*, `doppler_secret_rename`*, `doppler_audit_consolidation` |

`*` = mutating tool — requires `confirm=true`.

Total: ≥ 66 tools across 8 namespaces.

## Runtime
- **Language/runtime:** Python 3.x; FastMCP + asyncpg
- **Entry point:** `motto-mcp-server` (starts HTTP on `$PORT`, default 8080)
- **Hosting:** Northflank service `motto-mcp` in project `motto-agents`; hostname: `motto-mcp.northflank.app`
- **Schedule:** Always-on deployment

## Required environment variables
| Variable | Purpose | Source |
|---|---|---|
| `DATABASE_URL` | Neon Postgres connection string (`postgresql://...?sslmode=require`) | `motto-fleet-secrets` Northflank group / Doppler |
| `MOTTO_MCP_AUTH_TOKEN` | Bearer token for all non-healthz endpoints (generate: `openssl rand -hex 32`) | `motto-fleet-secrets` Northflank group / Doppler |
| `MOTTO_MCP_MOUNT_DOMAIN_SERVERS` | Set to `1` to mount cloudflare/northflank/github/linear/apollo/supabase/doppler/grabber sub-servers | Doppler `motto-core/prd` |
| `PORT` | HTTP listen port (Northflank sets automatically) | Northflank env |

### Domain sub-server credentials (read lazily; missing creds only fail the affected tool)
| Variable | Used by | Notes |
|---|---|---|
| `CLOUDFLARE_API_TOKEN` | `cloudflare__*` | Doppler also has legacy `CLOUDFLARE_API` |
| `NORTHFLANK_API_TOKEN` | `northflank__*` | Aliases: `NORTHFLANK_API_KEY`, legacy `NORHTFLANK_API` (typo) |
| `FLEET_PROVISION_PAT` | `github__set_secret` (write surface) | Falls back to `GITHUB_PAT` / `GITHUB_TOKEN` for read tools |
| `LINEAR_API_KEY` | `linear__*` | Personal API key (no `Bearer` prefix) |
| `APOLLO_API_KEY` | `apollo__*` | Sent as `X-Api-Key` header |
| `SUPABASE_URL` + `SUPABASE_SERVICE_KEY` | `supabase__*` | Aliases for the key: `SUPABASE_SERVICE_ROLE_KEY`, `SUPABASE_SECRET_KEY` |
| `DOPPLER_TOKEN` | `doppler__*` | Service token scoped to `motto-core/prd` |
| `MOTTO_DOPPLER_ALLOWLIST` | `doppler__read_secret` | Optional comma-separated override of the read allowlist |

## Doppler config
- Project: `motto-core`
- Config: `prd`
- Pull command: `doppler run --project motto-core --config prd -- <command>`

## How to run locally
```bash
cp .env.example .env   # fill in DATABASE_URL
pip install -e '.[dev]'
motto-mcp-server   # starts on :8080
# open http://localhost:8080/dashboard (no token needed in dev mode)
```

## How to deploy
Push to `main` → CI builds Docker image → Northflank auto-deploys. Schema applied automatically on first boot (idempotent).

Set `DATABASE_URL` and `MOTTO_MCP_AUTH_TOKEN` in Northflank shared secret group `motto-fleet-secrets`. Same `MOTTO_MCP_AUTH_TOKEN` value goes into every variable agent's secret group.

## Conventions
- Branch from `main`. PRs only. No direct pushes to main.
- Use DeepSeek V4 / Reasoner for code generation. Claude is banned from this fleet for cost reasons.
- One PR per logical change. Keep diffs minimal.
- Update this AGENTS.md if you change the architecture.

## Known issues / open loops
- Auth middleware was "a TODO" at v0 — verify current auth implementation before exposing externally.
- Dashboard accessible via `https://motto-mcp.northflank.app/dashboard?token=<token>` — bookmark with token.
- Deterministic services (`motto-appraisal-pipeline`, `motto-video-agent`) do NOT call this MCP — they use Langfuse OTel only.

## Maritime status
Maritime.sh is dead. This repo does not reference Maritime.
