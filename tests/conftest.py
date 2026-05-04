"""Shared fixtures for fleet MCP read-tool tests.

Tests need a live Postgres (Neon dev branch or a local instance). Set
NEON_TEST_DATABASE_URL to opt in; otherwise every test in this directory
is skipped.
"""

from __future__ import annotations

import os
from typing import Any
from uuid import UUID

import pytest

NEON_TEST_DATABASE_URL = os.environ.get("NEON_TEST_DATABASE_URL")

requires_db = pytest.mark.skipif(
    not NEON_TEST_DATABASE_URL,
    reason="NEON_TEST_DATABASE_URL not set; skipping DB-backed tests",
)


@pytest.fixture
async def db():
    """Connected, migrated Database instance bound to NEON_TEST_DATABASE_URL.

    Truncates fleet tables before each test so tests are independent.
    """
    if not NEON_TEST_DATABASE_URL:
        pytest.skip("NEON_TEST_DATABASE_URL not set")

    os.environ["DATABASE_URL"] = NEON_TEST_DATABASE_URL
    from mcp_server.db import Database

    db = Database()
    await db.connect()
    await db.apply_migrations()
    async with db.pool.acquire() as conn:
        # Order matters: events/decisions/artifacts/locks reference runs;
        # runs reference agents.
        await conn.execute(
            "TRUNCATE fleet.locks, fleet.artifacts, fleet.decisions, "
            "fleet.events, fleet.intents, fleet.runs, fleet.agents "
            "RESTART IDENTITY CASCADE"
        )
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
async def server(db):
    """A FastMCP server instance whose tools talk to the test DB."""
    # mcp_server.server constructs its own module-level Database; we
    # monkey-patch it to point at our test instance for the duration of
    # the fixture.
    from mcp_server import server as server_mod

    original = server_mod.db
    server_mod.db = db
    try:
        yield server_mod.mcp
    finally:
        server_mod.db = original


async def _call(server, name: str, **kwargs) -> Any:
    """Invoke a registered FastMCP tool by name. Mirrors the doppler harness."""
    tool = await server.get_tool(name)
    result = await tool.run(arguments=kwargs)
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


async def insert_agent(db, name: str, kind: str = "variable") -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO fleet.agents (name, kind, last_seen_at)
            VALUES ($1, $2, now())
            ON CONFLICT (name) DO UPDATE SET last_seen_at = now()
            RETURNING id
            """,
            name, kind,
        )


async def insert_run(
    db,
    *,
    agent_name: str,
    kind: str = "test_kind",
    intent: str | None = None,
    status: str = "running",
    parent_run_id: str | None = None,
) -> str:
    await insert_agent(db, agent_name)
    async with db.pool.acquire() as conn:
        rid = await conn.fetchval(
            """
            INSERT INTO fleet.runs (agent_id, parent_run_id, kind, intent, status)
            SELECT id, $2, $3, $4, $5 FROM fleet.agents WHERE name = $1
            RETURNING id
            """,
            agent_name,
            UUID(parent_run_id) if parent_run_id else None,
            kind, intent, status,
        )
        return str(rid)


async def insert_event(
    db, *, agent_name: str, run_id: str, kind: str = "ev", level: str = "info",
    payload: dict[str, Any] | None = None,
) -> int:
    await insert_agent(db, agent_name)
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO fleet.events (agent_id, run_id, kind, level, payload)
            SELECT id, $2, $3, $4, $5 FROM fleet.agents WHERE name = $1
            RETURNING id
            """,
            agent_name, UUID(run_id), kind, level, payload or {},
        )


async def insert_decision(
    db, *, agent_name: str, run_id: str | None, choice: str,
    rationale: str | None = None, payload: dict[str, Any] | None = None,
) -> int:
    await insert_agent(db, agent_name)
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO fleet.decisions (agent_id, run_id, choice, rationale, payload)
            SELECT id, $2, $3, $4, $5 FROM fleet.agents WHERE name = $1
            RETURNING id
            """,
            agent_name,
            UUID(run_id) if run_id else None,
            choice, rationale, payload or {},
        )


async def insert_artifact(
    db, *, agent_name: str, run_id: str, kind: str, name: str | None = None,
    content: dict[str, Any] | None = None,
) -> int:
    await insert_agent(db, agent_name)
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO fleet.artifacts (agent_id, run_id, kind, name, content)
            SELECT id, $2, $3, $4, $5 FROM fleet.agents WHERE name = $1
            RETURNING id
            """,
            agent_name, UUID(run_id), kind, name, content or {},
        )


async def insert_lock(
    db, *, resource: str, holder_run: str | None,
    expires_in_seconds: int = 600,
) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO fleet.locks (resource, holder_run, expires_at)
            VALUES ($1, $2, now() + make_interval(secs => $3))
            ON CONFLICT (resource) DO UPDATE SET
                holder_run = EXCLUDED.holder_run,
                expires_at = EXCLUDED.expires_at,
                acquired_at = now()
            """,
            resource,
            UUID(holder_run) if holder_run else None,
            expires_in_seconds,
        )
