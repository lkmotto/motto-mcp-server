# servers/doppler

Doppler MCP server for the Motto fleet. See [`SKILL.md`](./SKILL.md) for the agent-facing description.

## Install

```bash
pip install -e ".[doppler,dev]"
```

## Run

stdio (for Claude Code):

```bash
DOPPLER_TOKEN=... python -m servers.doppler
```

HTTP (inside the cluster):

```bash
MCP_TRANSPORT=http PORT=8081 DOPPLER_TOKEN=... python -m servers.doppler
```

## Test

```bash
pytest servers/doppler/tests
```

Tests run against an in-process `DopplerClient` mock — no live Doppler call.

## Layout

```
servers/doppler/
├── SKILL.md          # agent-facing description
├── README.md         # this file
├── __init__.py
├── __main__.py       # `python -m servers.doppler`
├── server.py         # FastMCP server + tool definitions
└── tests/
    └── test_server.py
```
