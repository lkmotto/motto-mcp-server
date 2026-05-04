# servers/grabber

Grabber MCP server for the Motto fleet. See [`SKILL.md`](./SKILL.md) for the agent-facing description.

## Install

```bash
pip install -e ".[dev]"
```

## Run

stdio (for Claude Code):

```bash
DATABASE_URL=postgres://... python -m servers.grabber
```

HTTP (inside the cluster):

```bash
MCP_TRANSPORT=http PORT=8084 DATABASE_URL=postgres://... python -m servers.grabber
```

## Test

```bash
pytest servers/grabber/ -q
```

## Env

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | Required. Neon Postgres connection string. Canonical home: Doppler `motto-core/prd`. |
| `MCP_TRANSPORT` | `stdio` (default) or `http`. |
| `PORT` | HTTP port when `MCP_TRANSPORT=http`. Default `8084`. |
| `LOG_LEVEL` | Standard Python logging level. Default `INFO`. |
| `GRABBER_FROZEN` | Set to `1` to report `frozen` status in `grabber_health`. Poller also checks this. |

## Schema

Migration `mcp_server/migrations/0003_grabber_jobs.sql` creates:

- `fleet.grabber_jobs` — the rotation job queue
- `fleet.grabber_playbooks` — admin-managed service whitelist

Applied automatically by `mcp_server.db.Database.apply_migrations()` at server startup.

## Triggering a rotation

### MCP path (preferred — from Perplexity chat or director)

```python
enqueue_rotation(service="anthropic", reason="scheduled weekly rotation")
# Returns: {job_id: "uuid", status: "pending"}
# The grabber poller picks it up within 30s.
```

### Manual path (dev / emergency)

```bash
python -m grabber rotate anthropic
```

This runs the Playwright rotation synchronously, without touching the job queue.

## Reference

- Schema: `mcp_server/migrations/0003_grabber_jobs.sql`
- Poller: `motto-credential-grabber/grabber/poller.py`
- Playbooks: `motto-credential-grabber/grabber/playbooks/`
