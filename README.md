# motto-mcp-server

FastMCP fleet-coordination server, backed by Neon Postgres.

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
   the same group.
3. Deploy this repo as a Docker service. Schema is applied automatically
   on first boot.

## Auth (v0)

This service has **no application-level auth in v0**. Deploy it inside
the Northflank private group so only fleet agents can reach it.

For public exposure later, add Starlette `BearerAuthMiddleware`
around `mcp.http_app()` checking `MOTTO_MCP_AUTH_TOKEN`. Sketch:

```python
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.path.startswith("/healthz"):
            return await call_next(request)
        if request.headers.get("authorization") != f"Bearer {EXPECTED}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)
```

## Local dev

```bash
cp .env.example .env  # fill DATABASE_URL
pip install -e '.[dev]'
motto-mcp-server  # starts on :8080
```

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
