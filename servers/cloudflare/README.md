# servers/cloudflare

Cloudflare MCP server for the Motto fleet. See [`SKILL.md`](./SKILL.md) for the agent-facing description.

## Install

```bash
pip install -e ".[dev]"
```

## Run

stdio (for Claude Code):

```bash
CLOUDFLARE_API_TOKEN=... python -m servers.cloudflare
```

HTTP (inside the cluster):

```bash
MCP_TRANSPORT=http PORT=8083 CLOUDFLARE_API_TOKEN=... \
    python -c "from servers.cloudflare.server import main; main()"
```

## Test

```bash
pytest servers/cloudflare/ -q
```

## Env

| Variable | Purpose |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Required. Bearer token. Canonical home: Doppler `motto-core/prd`. |
| `MCP_TRANSPORT` | `stdio` (default) or `http`. |
| `PORT` | HTTP port when `MCP_TRANSPORT=http`. Default `8083`. |
| `LOG_LEVEL` | Standard Python logging level. Default `INFO`. |

## Reference

- Cloudflare API v4: <https://developers.cloudflare.com/api/>
- Zone / DNS / Pages / Workers / KV / R2 endpoints used by this server are all under `https://api.cloudflare.com/client/v4`.
