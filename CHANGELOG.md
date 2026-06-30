# Changelog

All notable changes to motto-mcp-server will be documented in this file.

## [0.1.0] — Unreleased

### Added
- Initial release of motto-mcp-server: FastMCP fleet-coordination server backed by Neon Postgres.
- Agent registration, heartbeat, run lifecycle, event emission, and cross-agent intent signals.
- HTTP dashboard at `/dashboard` and JSON fleet status at `/fleet/status.json`.
- Interactive cockpit UI at `/cockpit` with chat and intent submission.
- Fleet proxy tools for pipeline and cockpit via private Northflank DNS.
- Credential rotation job queue (grabber MCP server).
- Doppler MCP server for secret access.
- Idempotent SQL migrations applied automatically on startup.
- Multi-server architecture: fleet server, grabber, and doppler MCPs.
