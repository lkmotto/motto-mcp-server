---
name: motto-cloudflare-mcp
description: "MCP tools for Cloudflare — list/inspect accounts, zones, DNS records, Workers, Pages projects/deployments, KV namespaces, and R2 buckets; create/delete DNS records, purge zone cache, and redeploy Pages. Used by motto-director to manage edge infrastructure for the motto domains without leaving the agent loop."
metadata:
  version: '1.0'
  hosts: ['motto-mcp-server']
---

# motto-cloudflare MCP

A FastMCP server that wraps the Cloudflare API for the Motto fleet.

## Tools

| Tool | Purpose | Mutates? |
| --- | --- | --- |
| `list_accounts` | List Cloudflare accounts visible to the token | no |
| `list_zones` | List zones (domains); optional account filter | no |
| `get_zone` | Fetch a zone by id | no |
| `list_dns_records` | List DNS records in a zone (filter by type/name) | no |
| `list_workers` | List Workers scripts in an account | no |
| `get_worker` | Fetch a Workers script's metadata | no |
| `list_pages_projects` | List Pages projects in an account | no |
| `get_pages_project` | Fetch a Pages project | no |
| `list_pages_deployments` | List deployments for a Pages project (newest first) | no |
| `list_kv_namespaces` | List Workers KV namespaces | no |
| `list_r2_buckets` | List R2 buckets | no |
| `create_dns_record` | Create a DNS record | yes — `confirm=true` required |
| `delete_dns_record` | Delete a DNS record | yes — `confirm=true` required |
| `purge_zone_cache` | Purge ALL Cloudflare cache for a zone | yes — `confirm=true` required |
| `redeploy_pages` | Trigger a fresh Pages deployment from a branch | yes — `confirm=true` required |

## Auth

One token, read from env at first tool call:

- `CLOUDFLARE_API_TOKEN` — canonical home is Doppler `motto-core/prd`. Inject at runtime via Doppler env injection or set explicitly.

If the token is unset when a tool runs, the server raises a clear `RuntimeError`. Import-time / pytest collection works without it.

The token needs whatever Cloudflare permissions the tools you use require — typically `Account: Read`, `Zone: Read`, `Zone: DNS: Edit` (for create/delete record), `Zone: Cache Purge: Purge` (for purge_zone_cache), and `Pages: Edit` (for redeploy_pages).

## Safety

- Every mutating tool requires `confirm=true`. Calling without it raises with the scope of the refused mutation so the caller can re-issue with confirm.
- Cloudflare wraps every API response in `{success, errors, messages, result}`. The server unwraps `result` on success and raises with the joined `errors` array on failure — even when Cloudflare returns 200 with `success: false` (some endpoints do this for partial failures).
- `purge_zone_cache` purges *everything* in the zone. This is intentional for the Motto fleet's small zones; if more granular purging is ever needed, add a `files` / `tags` parameter.

## Registering with Claude Code

```jsonc
{
  "mcpServers": {
    "motto-cloudflare": {
      "command": "python",
      "args": ["-m", "servers.cloudflare"],
      "env": {
        "CLOUDFLARE_API_TOKEN": "${CLOUDFLARE_API_TOKEN}"
      }
    }
  }
}
```

For HTTP transport (when motto-director calls this from inside the cluster), run with `python -c "from servers.cloudflare.server import main; main()"` and set `MCP_TRANSPORT=http`, `PORT=8083`.

## Common workflows

### Add an A record for a new subdomain

```
1. list_zones() → find the zone id for motto.app
2. create_dns_record(
       zone_id, type="A", name="staging",
       content="203.0.113.42", proxied=true, confirm=true
   )
```

### Promote a Pages preview to production

```
1. list_pages_deployments(account_id, project_name)
   → confirm the latest "production" deployment is healthy
2. redeploy_pages(account_id, project_name, branch="main", confirm=true)
```

### Purge cache after a content change

```
purge_zone_cache(zone_id, confirm=true)
```

### Audit DNS for a zone

```
1. list_dns_records(zone_id) → full list
2. list_dns_records(zone_id, type="TXT") → just verification records
```

## Out of scope

- Editing Workers script source — use `wrangler` from a deploy pipeline instead
- Creating / deleting accounts, zones, KV namespaces, R2 buckets — do these in the Cloudflare dashboard
- Reading Workers script bindings / secrets — write tools surface the metadata only
- Cloudflare Access / Zero Trust — different scope, not yet wired
