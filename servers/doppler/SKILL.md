---
name: motto-doppler-mcp
description: "MCP tools for Doppler secret management across the Motto workplace. Read, write, rename, and audit secrets in motto-core/prd (the canonical project) and across legacy projects. Use during the secret-consolidation effort and for routine credential rotation."
metadata:
  version: '1.0'
  hosts: ['motto-mcp-server']
---

# motto-doppler MCP

A FastMCP server that wraps the Doppler REST API for the Motto fleet.

## Tools

| Tool | Purpose | Mutates? |
| --- | --- | --- |
| `doppler_projects_list` | List every Doppler project | no |
| `doppler_configs_list` | List configs for a project | no |
| `doppler_secrets_list` | List secrets in a project/config (values opt-in) | no |
| `doppler_secret_get` | Read one secret (raw + computed) | no |
| `doppler_secret_set` | Create/update a secret | yes — `confirm=true` required |
| `doppler_secret_delete` | Delete a secret | yes — `confirm=true` required |
| `doppler_secret_rename` | Rename a secret (copy→delete) | yes — `confirm=true` required |
| `doppler_audit_consolidation` | Cross-project dedupe report (hash-only, no plaintext) | no |

## Defaults

The default scope on every tool is `motto-core` / `prd` (the canonical project, per the May 2026 consolidation). Override `project` / `config` per-call when you need to touch a legacy project (e.g. to rename a secret out of it before deleting).

## Auth

Two tokens, both via Doppler-injected env:

- `DOPPLER_TOKEN` — service token scoped to `motto-core/prd` (read+write). Used by every read of motto-core and every write.
- `DOPPLER_AUDIT_TOKEN` — personal token with read access to ALL projects. Used by `doppler_audit_consolidation` and `doppler_projects_list`. Falls back to `DOPPLER_TOKEN` if absent (audit will silently skip projects the service token can't see).

## Safety

- Every mutating tool requires `confirm=true`. Calling without it returns a dry-run preview.
- `doppler_audit_consolidation` returns SHA-256 hashes of values, never plaintext. This is intentional — chat transcripts are not a place for plaintext secrets.

## Registering with Claude Code

```jsonc
{
  "mcpServers": {
    "motto-doppler": {
      "command": "python",
      "args": ["-m", "servers.doppler"],
      "env": {
        "DOPPLER_TOKEN": "${DOPPLER_TOKEN}",
        "DOPPLER_AUDIT_TOKEN": "${DOPPLER_AUDIT_TOKEN}"
      }
    }
  }
}
```

For HTTP transport (when motto-director calls this from inside the cluster), set `MCP_TRANSPORT=http` and `PORT=8081`.

## Common workflows

### Audit and consolidate

```
1. doppler_audit_consolidation()
   → returns 'duplicates' with values_match true/false
2. for each duplicate where values_match=true:
       doppler_secret_delete(project=<non-canonical>, name=..., confirm=true)
3. for each duplicate where values_match=false:
       human review — pick the right value, then set in motto-core/prd
```

### Canonical rename

```
doppler_secret_rename(
  old_name="CLAUDE_OAUTH_TOKEN",
  new_name="CLAUDE_CODE_OAUTH_TOKEN",
  confirm=true,
)
```

### Read for a runbook

```
doppler_secret_get(name="GITHUB_TOKEN")
   → {raw, computed, note, ...}
```

## Out of scope

- Doppler webhooks (use the dashboard)
- Service-token rotation (do this manually for now; trail in the agent-ops skill)
- Cross-workplace operations (we have one workplace)
