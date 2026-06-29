"""Motto Cockpit -- centralized control UI for the agent fleet.

This module is a compatibility shim. The original 1,780-line monolith has been
split into five focused modules:

    mcp_server.auth.py              -- token/credential handling (~20 lines)
    mcp_server.fleet_context.py     -- fleet metadata builder (~41 lines)
    mcp_server.handlers.deepseek.py -- DeepSeek LLM client (~268 lines)
    mcp_server.routes               -- HTTP route registration (~480 lines)
    mcp_server.templates            -- HTML template rendering (~896 lines)

The public API (``register_routes``) is re-exported here for backward
compatibility with ``server.py``.
"""

from .routes import register_routes  # noqa: F401
