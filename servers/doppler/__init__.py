"""Motto Doppler MCP server.

Exposes Doppler secrets-management operations as MCP tools so Claude Code
sessions and motto-director can audit, read, write, and rename secrets
across the Motto Doppler workplace without copy-pasting CLI commands.

Canonical project: ``motto-core`` config ``prd``. The Doppler MCP is the
operational arm of the May 2026 secret-consolidation effort
(see lkmotto/motto-director#7).
"""

from .server import build_server

__all__ = ["build_server"]
