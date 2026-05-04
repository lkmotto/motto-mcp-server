"""Grabber MCP server — job queue and rotation management for the credential rotator.

Exposes enqueue/list/get/cancel rotation jobs, playbook listing, and health
summaries. All tools talk to Neon Postgres (``DATABASE_URL``). Tools raise with
a clear error when the DB is not configured.

Run with ``python -m servers.grabber`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.
"""

from .server import main  # noqa: F401
