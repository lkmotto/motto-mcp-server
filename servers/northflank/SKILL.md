---
name: motto-northflank-mcp
description: "MCP tools for Northflank deployment management — list/inspect projects, services, cron jobs, and secret groups; restart/redeploy services, trigger job runs, and resync secret groups. Used by motto-director to nudge deployments without leaving the agent loop."
metadata:
  version: '1.0'
  hosts: ['motto-mcp-server']
---

# motto-northflank MCP

A FastMCP server that wraps the Northflank REST API for the Motto fleet.

## Tools

| Tool | Purpose | Mutates? |
| --- | --- | --- |
| `list_projects` | List all Northflank projects | no |
| `get_project` | Fetch a project by id | no |
| `list_services` | List services in a project | no |
| `get_service` | Fetch a service | no |
| `list_jobs` | List cron jobs in a project | no |
| `get_job` | Fetch a cron job | no |
| `list_secret_groups` | List secret groups (metadata only) | no |
| `get_secret_group` | Fetch a secret group's metadata | no |
| `get_recent_logs` | Last N lines of a service's logs | no |
| `restart_service` | Restart a service | yes — `confirm=true` required |
| `redeploy_service` | Rebuild + redeploy a service | yes — `confirm=true` required |
| `trigger_job_run` | Ad-hoc run of a cron job | yes — `confirm=true` required |
| `resync_secret_group` | Force a secret group to resync (e.g. after a Doppler change) | yes — `confirm=true` required |

## Auth

One token, read from env at first tool call:

- `NORTHFLANK_API_TOKEN` — canonical home is Doppler `motto-core/prd`. Inject at runtime via Doppler env injection or set explicitly.

If the token is unset when a tool runs, the server raises a clear `RuntimeError`. Import-time / pytest collection works without it.

## Safety

- Every mutating tool requires `confirm=true`. Calling without it raises with the scope of the refused mutation so the caller can re-issue with confirm.
- Read tools never expose secret-group values — only metadata. To read values, use `motto-doppler` against the canonical Doppler project instead.

## Registering with Claude Code

```jsonc
{
  "mcpServers": {
    "motto-northflank": {
      "command": "python",
      "args": ["-m", "servers.northflank"],
      "env": {
        "NORTHFLANK_API_TOKEN": "${NORTHFLANK_API_TOKEN}"
      }
    }
  }
}
```

For HTTP transport (when motto-director calls this from inside the cluster), run with `python -c "from servers.northflank.server import main; main()"` and set `MCP_TRANSPORT=http`, `PORT=8082`.

## Common workflows

### Nudge a wedged service

```
1. get_recent_logs(project_id, service_id, lines=200)
   → look at the tail; confirm it's actually wedged
2. restart_service(project_id, service_id, confirm=true)
   → returns {"restarted": true, ...}
3. wait ~30s, get_recent_logs again to verify recovery
```

### Roll a Doppler-injected secret

```
1. (via motto-doppler) doppler_secret_set(name=..., value=..., confirm=true)
2. resync_secret_group(project_id, group_id, confirm=true)
3. redeploy_service(project_id, service_id, confirm=true)
```

### Force a cron tick (debug)

```
trigger_job_run(project_id, job_id, confirm=true)
```

## Out of scope

- Creating / deleting projects, services, or jobs — do these in the Northflank UI
- Editing service spec (image, env, scale) — UI / Northflank IaC
- Webhook / build-trigger management
