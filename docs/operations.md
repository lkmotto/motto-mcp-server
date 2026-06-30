# motto-mcp-server — Operations

## Service Overview

- **Service**: motto-mcp-server
- **Kind**: Always-on HTTP + MCP server
- **Hosting**: Northflank project `motto-agents`, service `motto-mcp-server`
- **Hostname**: `motto-mcp.northflank.app`
- **Port**: `$PORT` (set by Northflank, default 8080 locally)
- **Entry point**: `motto-mcp-server` (starts FastMCP HTTP server)
- **Owner**: Luke Motto (`ljm32901@gmail.com`)

The MCP server is the central nervous system for the Motto fleet. Variable agents register, heartbeat, open/close runs, emit events, and post cross-agent intent signals. The motto-director reads fleet state via `get_fleet_status` and `get_recent_events` for its perceive step. Also serves an HTML dashboard and JSON status endpoint. Backed by Neon Postgres; schema is auto-applied on boot.

## Health Check

- **Endpoint**: `GET /healthz`
- **Response**: HTTP 200 with body `ok` (plain text)
- **Auth**: Open (no token required)
- **Purpose**: Liveness probe for Northflank health monitoring and load balancer health checks.

```bash
# Local
curl http://localhost:8080/healthz

# Production
curl https://motto-mcp.northflank.app/healthz
```

The health check endpoint does not require auth and does not touch the database — it simply confirms the HTTP server is accepting connections. For a deeper liveness check, use:

```bash
curl -H "Authorization: Bearer $MOTTO_MCP_AUTH_TOKEN" \
  https://motto-mcp.northflank.app/fleet/status.json
```

This endpoint returns the fleet status (agents registered, recent events) and confirms database connectivity.

## Smoke Test

```bash
python scripts/smoke_test.py
```

The smoke test verifies:
1. All core `mcp_server` modules import without errors
2. `motto_common.sentry_init` is importable and `init_sentry` works
3. The `/healthz` endpoint returns 200 when the server is running
4. The `/fleet/status.json` endpoint returns valid JSON (when auth is configured)
5. The server can start and bind its port within 15 seconds (when `DATABASE_URL` is set)

## SLIs (Service Level Indicators)

| Indicator | Description | Measurement |
|---|---|---|
| **Uptime / availability** | Percentage of time the service responds to `/healthz` | Northflank health check probes |
| **Request latency (p50, p95, p99)** | End-to-end latency for MCP tool invocations | FastMCP server metrics + Sentry traces |
| **Error rate** | Percentage of MCP tool calls returning errors | Sentry error events / total tool invocations |
| **Database connectivity** | Percentage of time the Neon Postgres connection is healthy | Connection pool errors captured by Sentry |
| **Tool invocation success rate** | Percentage of tool calls that return successfully | Per-tool: register_agent, heartbeat, record_run_start, etc. |
| **Auth rejection rate** | Rate of 401 responses to protected endpoints | Server access logs |

## SLOs (Service Level Objectives)

| Objective | Target | Window |
|---|---|---|
| Availability (healthz success) | >= 99.5% | Rolling 30d |
| Request latency p95 | <= 500ms | Rolling 1h |
| Request latency p99 | <= 2s | Rolling 1h |
| Error rate | <= 1% of all requests | Rolling 1h |
| Database connectivity | >= 99.9% | Rolling 30d |
| Sentry error budget | <= 50 error events per 24h | Rolling 24h |

## Runbook

### Deployment

The MCP server deploys automatically on merge to `main` via `.github/workflows/deploy.yml`. The workflow:
1. Triggers a Northflank build for the `motto-mcp-server` service image
2. Polls the build until it succeeds or fails (capped at 12 minutes)
3. Deploys the built image — the running service rolls over to the new image

**Manual deploy** (if CI is unavailable):
```bash
export COMMIT_SHA=$(git rev-parse HEAD)
curl -X POST \
  -H "Authorization: Bearer $NORTHFLANK_API_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"sha\":\"$COMMIT_SHA\"}" \
  "https://api.northflank.com/v1/projects/motto-agents/services/motto-mcp-server/build"
```

**Release deploy** (tag-based):
```bash
git tag v0.2.0
git push origin v0.2.0
# Triggers .github/workflows/release.yml: runs tests, bandit, updates CHANGELOG,
# triggers Northflank build+deploy, and creates a GitHub Release.
```

### Rollback

1. **Via Northflank dashboard**: Navigate to the `motto-mcp-server` service, select a previous build from build history, and redeploy it. The service rolls over gracefully.

2. **Via git revert**:
   ```bash
   git revert <bad-commit-sha>
   git push origin main
   # CI triggers deploy of the reverted version
   ```

3. **Emergency rollback to last known good tag**:
   ```bash
   git checkout v0.1.0
   # Trigger Northflank build manually with this SHA
   ```

### Incident Response

**Symptom: Service is down (/healthz returns non-200)**
1. Check Northflank service dashboard — verify the service is running.
2. Check recent deployments — did a recent deploy introduce a regression?
3. Check Northflank logs for the `motto-mcp-server` service — look for startup errors.
4. Verify `DATABASE_URL` is valid and Neon Postgres is reachable.
5. Check if `PORT` environment variable is set correctly by Northflank.

**Symptom: High request latency**
1. Check Sentry performance traces — identify slow tool invocations.
2. Check Neon Postgres dashboard for query performance — slow queries on fleet tables.
3. Verify the Northflank service has sufficient resources (CPU/RAM).
4. Check if `get_recent_events` queries are scanning large time windows — limit `since_minutes`.

**Symptom: Database connection errors**
1. Check Neon Postgres status — verify the database is running and not in idle scale-down.
2. Verify `DATABASE_URL` is correct and includes `?sslmode=require`.
3. Check if the connection pool is exhausted — the server uses a single connection for simplicity; consider pooling if concurrent agents increase.
4. The schema is applied idempotently on boot — check migration logs for errors.

**Symptom: Auth failures (401 on all protected endpoints)**
1. Verify `MOTTO_MCP_AUTH_TOKEN` is set and matches across all consumers (director, agents).
2. The token is in Northflank secret group `motto-fleet-secrets`. Verify it synced from Doppler.
3. If the token was rotated, update it in ALL consumer secret groups simultaneously.
4. For local dev, leave `MOTTO_MCP_AUTH_TOKEN` unset — all endpoints are open.

**Symptom: Agents cannot register (register_agent failures)**
1. Check that `kind` parameter is either `variable` or `deterministic` — other values are rejected.
2. Check the agent is calling the correct MCP URL (`MOTTO_MCP_URL`).
3. Verify the agent's `MOTTO_MCP_AUTH_TOKEN` matches the server's token.
4. Check the `agents` table in Neon — `register_agent` is idempotent (upserts).

**Symptom: Sentry errors not appearing**
1. Verify `SENTRY_DSN` is set in the Northflank secret group `motto-fleet-secrets`.
2. Check the Sentry project `motto-mcp-server` exists in the `lkmotto` organization.
3. Verify `motto_common.sentry_init` is importable — run `python scripts/smoke_test.py`.
4. Check that `init_sentry(agent_name="motto-mcp-server")` is called in `mcp_server/server.py`.

### Monitoring & Alerting

- **Sentry**: Error tracking with release health (every deploy registers the git SHA as the release version). Alert rules configured via `scripts/configure_sentry_alerts.py`.
- **Northflank**: Service health monitoring with automatic restarts on failure.
- **Postgres**: Neon dashboard for query performance, connection counts, and storage.

### Data & Backups

- **Database**: Neon Postgres (serverless). The `mcp_server/migrations/` directory contains idempotent SQL migrations applied automatically on boot.
- **Schema**: Tables: `agents`, `runs`, `events`, `intents`, `artifacts`, `decisions`, `locks`, `local_tasks`, `pending_moves`, `verifications`, `capability_requests`, `trust_scores`.
- **Backups**: Neon provides point-in-time recovery. No additional backup configuration is needed.
- **Data retention**: Events older than 30 days may be pruned. Core fleet state (agents, runs) is retained indefinitely.

### Configuration

All runtime configuration is via environment variables. See `AGENTS.md` for the complete list. Critical variables:

| Variable | Purpose | Source |
|---|---|---|
| `DATABASE_URL` | Neon Postgres connection string | Northflank `motto-fleet-secrets` |
| `MOTTO_MCP_AUTH_TOKEN` | Bearer token for protected endpoints | Northflank `motto-fleet-secrets` |
| `PORT` | HTTP listen port | Northflank (auto-set) |
| `SENTRY_DSN` | Sentry error tracking | Northflank `motto-fleet-secrets` |
| `MOTTO_MCP_MOUNT_DOMAIN_SERVERS` | Set to `1` to mount grabber MCP | Optional |

Secrets are managed via Northflank secret groups. The `MOTTO_MCP_AUTH_TOKEN` must be identical across the MCP server and all consumer agents.

### Local Development

```bash
# Install dependencies
cd motto-mcp-server
cp .env.example .env   # fill in DATABASE_URL
uv sync

# Start server
motto-mcp-server   # starts on :8080

# Run smoke test
python scripts/smoke_test.py

# Check health
curl http://localhost:8080/healthz

# Open dashboard (no token needed in dev mode)
open http://localhost:8080/dashboard

# Run tests
uv run pytest

# Run integration tests
uv run pytest tests/integration/ -v
```
