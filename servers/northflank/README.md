# servers/northflank

Northflank MCP server for the Motto fleet. See [`SKILL.md`](./SKILL.md) for the agent-facing description.

## Install

```bash
pip install -e ".[dev]"
```

## Run

stdio (for Claude Code):

```bash
NORTHFLANK_API_TOKEN=... python -m servers.northflank
```

HTTP (inside the cluster):

```bash
MCP_TRANSPORT=http PORT=8082 NORTHFLANK_API_TOKEN=... \
    python -c "from servers.northflank.server import main; main()"
```

## Test

```bash
pytest servers/northflank/tests
```

Tests run against an in-process `NorthflankClient` subclass — no live Northflank call.

## Env

- `NORTHFLANK_API_TOKEN` — required at runtime; canonical home is Doppler `motto-core/prd`.
- `MCP_TRANSPORT` — `stdio` (default) or `http` (only when invoked through `server.main()`).
- `PORT` — HTTP transport port, default `8082`.
- `LOG_LEVEL` — default `INFO`.

## API reference

Northflank REST API: <https://api.northflank.com/v1> (Bearer auth).

## Layout

```
servers/northflank/
├── SKILL.md          # agent-facing description
├── README.md         # this file
├── __init__.py
├── __main__.py       # `python -m servers.northflank`
├── server.py         # FastMCP server + tool definitions
└── tests/
    └── test_server.py
```
