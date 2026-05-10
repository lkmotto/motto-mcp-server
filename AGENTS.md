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
- `mcp_server/server.py` — FastMCP server; HTTP surface (`/mcp/`, `/dashboard`, `/cockpit`, `/fleet/status.json`, `/healthz`)
- `mcp_server/db.py` — Neon Postgres connection + query helpers
- `mcp_server/tools_fleet_proxy.py` — Fleet proxy tools (pipeline + cockpit via private Northflank DNS)
- `mcp_server/cockpit.py` — Interactive cockpit UI (chat + intent submit)
- `mcp_server/chat_tools.py` — Director chat integration
- `mcp_server/migrations/` — Idempotent SQL migrations applied on startup
- `servers/grabber/` — grabber MCP server (credential rotation job queue)
- `pyproject.toml` — Package; entry point: `motto-mcp-server`
- `Dockerfile` — Container image for Northflank

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
| `PORT` | HTTP listen port (Northflank sets automatically) | Northflank env |

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
