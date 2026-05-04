---
name: motto-grabber-mcp
description: "MCP tools for the Motto credential-rotation job queue — enqueue rotations, list and inspect jobs, cancel pending rotations, list playbooks, and check subsystem health. Used by motto-director and Perplexity chats to trigger and monitor credential rotations without manual intervention."
metadata:
  version: '1.0'
  hosts: ['motto-mcp-server']
---

# motto-grabber MCP

A FastMCP server that wraps the `fleet.grabber_jobs` and `fleet.grabber_playbooks` tables in Neon Postgres, exposing the credential-rotation lifecycle as MCP tools.

## Tools

| Tool | Purpose | Mutates? |
| --- | --- | --- |
| `enqueue_rotation` | Schedule a credential rotation for a whitelisted service | yes — inserts a pending job |
| `list_rotations` | List jobs newest-first, optional status filter | no |
| `get_rotation` | Fetch a single job with duration_ms and audit link | no |
| `list_playbooks` | Show configured services (names, keys — never values) | no |
| `cancel_rotation` | Cancel a pending job; no-op if running/done | yes — updates status |
| `grabber_health` | Lightweight health summary (queued, running, last run) | no |

## Auth

Connection string read from `DATABASE_URL` at first tool call:

- `DATABASE_URL` — Neon Postgres connection string. Canonical home: Doppler `motto-core/prd`.

If `DATABASE_URL` is unset, every tool raises a clear `RuntimeError`. Import-time / pytest collection works without it.

## Safety

- **No credential values are ever returned.** `list_rotations`, `get_rotation`, and `grabber_health` return only metadata fields. Payload / evidence columns do not exist in the response schema.
- `enqueue_rotation` validates `service` against `fleet.grabber_playbooks` (admin-managed). Unknown or disabled services are rejected before any job row is inserted.
- `cancel_rotation` is idempotent — safe to call multiple times.
- The poller (in `motto-credential-grabber`) runs each job under `asyncio.wait_for(timeout=300s)`; on timeout it sets `status='failed'`, `error_class='TimeoutError'`.
- If `GRABBER_FROZEN=1` is set in the poller environment, the poller sleeps and skips; `grabber_health` reflects this as `"status": "frozen"`.

## Registering with Claude Code

```jsonc
{
  "mcpServers": {
    "motto-grabber": {
      "command": "python",
      "args": ["-m", "servers.grabber"],
      "env": {
        "DATABASE_URL": "${DATABASE_URL}"
      }
    }
  }
}
```

For HTTP transport (director inside the cluster):

```bash
MCP_TRANSPORT=http PORT=8084 DATABASE_URL=... python -m servers.grabber
```

## Common workflows

### Schedule a rotation (preferred path)

```
enqueue_rotation(service="anthropic", reason="scheduled weekly rotation")
→ {job_id: "...", status: "pending"}
```

The poller in `motto-credential-grabber` picks it up within 30 s.

### Monitor progress

```
list_rotations(status="running")
get_rotation(job_id="...")
```

### Abort before the poller claims it

```
cancel_rotation(job_id="...", reason="wrong service, re-enqueueing")
```

### Morning digest

```
grabber_health()
→ {status: "ok", queued: 0, running: 0, last_run_at: "...", last_run_status: "succeeded", frozen: false}
```

## Playbook whitelist

Services must be registered in `fleet.grabber_playbooks` (admin-managed) before rotations can be enqueued. The migration seeds `anthropic` in `placeholder` state.

| Field | Meaning |
| --- | --- |
| `service` | Primary key, matches the grabber playbook module name |
| `dashboard_url` | Human-readable link to the vendor's API key dashboard |
| `target_doppler_keys` | Array of Doppler secret names that will be updated |
| `last_validated_at` | When the playbook was last tested end-to-end |
| `status` | `live` (fully automated), `placeholder` (needs manual review), `disabled` |

## Out of scope

- Viewing or extracting credential values — the grabber poller handles that in an ephemeral, zero-retention process
- Creating or modifying playbooks — done via direct DB migration or admin tooling
- Triggering the Playwright browser directly — that is `python -m grabber rotate <service>` for dev/manual use only
