# Deploy `motto-mcp-server` and register it as a Perplexity custom connector

End-to-end runbook for getting the fleet MCP server live on Northflank, then
plugged into Perplexity as a custom remote connector so chat sessions can
read fleet state, query Doppler, manage Northflank, and operate Cloudflare
on demand.

This is the **long-term path**. Once it's done, this Perplexity chat (and
any other MCP-compatible client) speaks to the same MCP surface that
director, sdr, and pipeline already use internally.

---

## What you're deploying

A single FastMCP HTTP service exposing four tool namespaces:

| Namespace  | Tools | Source           |
| ---------- | ----- | ---------------- |
| Fleet      | 7     | `mcp_server/`    |
| Doppler    | 4     | `servers/doppler/` |
| Northflank | 13    | `servers/northflank/` |
| Cloudflare | 15    | `servers/cloudflare/` |

The fleet server (`mcp_server/server.py`) is the canonical entrypoint. The
others are mounted alongside via FastMCP's multi-server pattern when
`MOTTO_MCP_MOUNT_DOMAIN_SERVERS=1`.

`/healthz` is unauthenticated. Everything else (including `/mcp/`, the
streamable-HTTP transport endpoint Perplexity will hit) requires a Bearer
token.

---

## Prerequisites

- A Northflank project (already in place: `motto`)
- A Doppler config `motto-core/prd` with the env vars below populated
- A Neon database with the fleet schema applied (auto-applied on server start, but the DSN must be reachable)

### Required env vars (set on the Northflank service via Doppler integration or secret group)

| Var                     | Purpose                                           |
| ----------------------- | ------------------------------------------------- |
| `NEON_DATABASE_URL`     | Fleet control plane DB                            |
| `MOTTO_MCP_AUTH_TOKEN`  | Bearer token clients must send. **Generate fresh** — `openssl rand -hex 32` |
| `DOPPLER_TOKEN`         | Service-account token, read-only on `motto-core` (used by Doppler tools)    |
| `NORTHFLANK_API_KEY`    | Used by Northflank tools                          |
| `CLOUDFLARE_API_TOKEN`  | Used by Cloudflare tools                          |
| `CLOUDFLARE_ACCOUNT_ID` | Used by Cloudflare tools                          |
| `PORT`                  | Northflank injects automatically; default `8080`  |
| `MOTTO_MCP_MOUNT_DOMAIN_SERVERS` | `1` to mount Doppler/Northflank/Cloudflare alongside fleet |

`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`, `LANGFUSE_*`
are optional — server still works without them, just no LLM observability.

---

## Step 1 — Create the Northflank service

In the Northflank dashboard:

1. **+ Create new** → **Combined service** (build + deploy from a Git source)
2. **Repository**: `lkmotto/motto-mcp-server`, branch `main`
3. **Build**: Dockerfile (`./Dockerfile`)
4. **Resources**: `nf-compute-10` is plenty (256 MB RAM, 0.1 vCPU)
5. **Networking**:
   - **Public networking**: enabled
   - **Port**: `8080`, protocol `HTTP`
   - **External domain**: pick the auto-generated `*.northflank.app` URL or add your own — both work for Perplexity
6. **Environment variables**: link the secret group `motto-core-prd` (or set the vars listed above directly)
7. Click **Create**

When the build finishes and the deployment turns green, hit the public URL
plus `/healthz`:

```bash
curl https://<your-service>.northflank.app/healthz
# {"status":"ok"}
```

---

## Step 2 — Verify the auth gate

Without a token:

```bash
curl -i https://<your-service>.northflank.app/mcp/
# HTTP/1.1 401 Unauthorized
```

With the token:

```bash
curl -i \
  -H "Authorization: Bearer $MOTTO_MCP_AUTH_TOKEN" \
  https://<your-service>.northflank.app/mcp/
# HTTP/1.1 200 OK   (FastMCP streamable-http handshake)
```

If 401 with the right token: re-check that the Northflank service env var
matches what you're sending byte-for-byte (Doppler trims trailing newlines,
shell `$VAR` expansion does not).

---

## Step 3 — Register as a Perplexity custom connector

1. In Perplexity, go to **Account settings → Connectors**.
2. Click **+ Custom connector** (top-right).
3. Choose **Remote**.
4. Fill in:

   | Field             | Value                                                    |
   | ----------------- | -------------------------------------------------------- |
   | **Name**          | `Motto Fleet MCP`                                        |
   | **MCP Server URL**| `https://<your-service>.northflank.app/mcp/`             |
   | **Description**   | `Fleet runs/events/decisions, Doppler secrets, Northflank deploys, Cloudflare DNS/Workers/Pages.` |
   | **Authentication**| `API Key`                                                |
   | **API Key**       | The same `MOTTO_MCP_AUTH_TOKEN` value                    |
   | **Transport**     | `Streamable HTTP`                                        |
   | **Icon**          | optional (must be ≤128 KB)                               |

5. Check the acknowledgement box and click **Add**.
6. The connector card appears under Account settings → Connectors. Click it
   once to enable it for this Perplexity space.

After that, this chat (and every future chat in this space) can call
`list_runs`, `get_run`, `read_secret`, `list_dns_records`, etc. directly.

---

## Step 4 — Smoke test from Perplexity

Open a fresh chat and ask:

> List the last 5 fleet runs.

Computer should call `list_runs` (limit=5) and return a small table.

If it doesn't see the connector, check Account settings → Connectors → the
connector card is **enabled** (toggle on). On first enable Perplexity will
trigger a brief MCP handshake to discover the tools — there's a small
delay before they appear in tool listings.

---

## Step 5 — Lock it down (recommended)

- **Rotate `MOTTO_MCP_AUTH_TOKEN` after first successful connect** so the
  initial value (which may have appeared in deploy logs) is invalid.
- **Restrict the Northflank service to a Cloudflare-fronted hostname** if
  you want IP allowlisting on the underlying origin — not strictly needed
  since auth is enforced.
- **Watch `/healthz` from Northflank's built-in monitoring** so the service
  auto-restarts if the lifespan handler bombs.

---

## Operations

### Logs

```bash
# Last 100 lines of stdout (structured JSON events)
nf logs --service motto-mcp-server --tail 100
```

Each tool call emits:

```json
{"event":"mcp.tool_call","tool":"list_runs","run_id":"...","duration_ms":12,"status":"ok"}
```

No tool arguments or return values are logged by default — to debug a
specific failure, set `MOTTO_MCP_DEBUG=1` (do this temporarily; it's
verbose).

### Schema migrations

`mcp_server/migrations/*.sql` are applied automatically on startup. To add
a migration:

1. Add `migrations/000N_description.sql` (forward-only)
2. Push to main
3. Northflank rebuilds + redeploys → migration runs once on the new
   container's startup
4. Verify in Neon: `select * from schema_migrations order by version desc limit 5;`

### Adding a new domain server (the path forward to 5+ for Horizon)

1. Create `servers/<service>/server.py` mirroring `servers/cloudflare/server.py`
2. Mount it from `mcp_server/server.py` under `mount=True`
3. Add the service token to env vars + Doppler
4. Bump version, push, redeploy
5. Update Perplexity connector — no action needed; new tools auto-discover
   on next handshake

Once 5+ domain servers are mounted, Phase 5.11 (Horizon MCP unification)
becomes meaningful: a single Horizon-managed front door routes by tool
namespace and centralizes logging/auth/rate-limiting.
