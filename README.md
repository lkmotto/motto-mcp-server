# motto-mcp-server

[![codecov](https://codecov.io/gh/lkmotto/motto-mcp-server/branch/main/graph/badge.svg)](https://codecov.io/gh/lkmotto/motto-mcp-server)

FastMCP fleet-coordination server, backed by Neon Postgres. Also exposes a
minimal HTML dashboard so a browser tab can serve as a poor-man's Horizon.

Variable agents in the motto fleet (`motto-director`, `motto-sdr-agent`,
`motto-social-agent`) call this MCP to:

- register themselves (`register_agent`)
- emit periodic heartbeats (`heartbeat`)
- open and close runs (`record_run_start`, `record_run_end`)
- record fine-grained events (`record_event`)
- post cross-agent nudges (`signal_intent`) and consume them (`consume_open_intents`)

`motto-director` reads the fleet via `get_fleet_status` and
`get_recent_events` to drive its `perceive → ideate → act` autonudge loop.

Deterministic services (`motto-appraisal-pipeline`, `motto-video-agent`)
do **not** call this MCP. They emit OpenTelemetry traces to Langfuse only.

## HTTP surface

| Path | Purpose | Auth |
|------|---------|------|
| `/mcp/` | FastMCP streamable-http transport (JSON-RPC) | required |
| `/dashboard` | Read-only HTML fleet view, refreshes every 30s | required |
| `/fleet/status.json` | Same data as dashboard, JSON | required |
| `/healthz` | Liveness probe | open |

Auth gate accepts EITHER:
- `Authorization: Bearer <MOTTO_MCP_AUTH_TOKEN>` (preferred, used by MCP clients)
- `?token=<MOTTO_MCP_AUTH_TOKEN>` (works for browser dashboard — just bookmark
  `https://motto-mcp.northflank.app/dashboard?token=<token>`)

If `MOTTO_MCP_AUTH_TOKEN` is unset, all paths are open (dev/local mode).

## Schema

```
fleet.agents       — every agent in the fleet (variable | deterministic)
fleet.runs         — units of work, optionally linked to a Langfuse trace
fleet.events       — fine-grained events within a run (or standalone)
fleet.intents      — cross-agent nudges, open / consumed / dismissed
fleet.schema_migrations — applied migration tracking
```

Migrations live in `mcp_server/migrations/` and are applied on startup
(idempotent — re-applying is a no-op).

## Deploy

Northflank, similar shape to the rest of the fleet:

1. Provision a Neon project (`motto_fleet` recommended database name)
   and put the connection string into the shared `motto-fleet-secrets`
   Doppler/Northflank secret group as `DATABASE_URL`.
2. Generate `MOTTO_MCP_AUTH_TOKEN` (`openssl rand -hex 32`) and add to
   the same group. **Same value** for this server (it validates) AND for
   each variable agent's group (they send it as bearer).
3. Deploy this repo as a Docker service. Schema is applied automatically
   on first boot.
4. Open `https://<your-mcp-host>/dashboard?token=<token>` in a browser
   and bookmark it. That's your Horizon.

## Local dev

```bash
cp .env.example .env  # fill DATABASE_URL
pip install -e '.[dev]'
motto-mcp-server  # starts on :8080
```

Then `open http://localhost:8080/dashboard` (no token needed in dev mode).

## Tools (v0)

| Tool | Purpose |
|------|---------|
| `register_agent(name, kind, deploy_target?, version?)` | Idempotent registration. |
| `heartbeat(agent_name, status?)` | Update `last_seen_at`, merge status into metadata. |
| `record_run_start(agent_name, kind, intent?, langfuse_trace_id?, parent_run_id?)` | Open a run, return `run_id`. |
| `record_run_end(run_id, status, summary?)` | Close a run. `success` / `error` / `cancelled`. |
| `record_event(agent_name, kind, payload?, run_id?, level?)` | Emit a fleet event. |
| `signal_intent(target_agent, kind, payload?, source_agent?)` | Post a cross-agent nudge. |
| `consume_open_intents(agent_name, limit?)` | Atomically claim open intents. |
| `get_fleet_status()` | Snapshot of every agent. |
| `get_recent_events(since_minutes?, agent_name?, kind?, limit?)` | Recent events, newest first. |
