"""asyncpg-backed Postgres queries for the Motto fleet schema.

A single `Database` instance owns a connection pool, scoped to the FastMCP
server lifespan. JSONB is encoded/decoded as Python dicts via a per-connection
codec, so callers pass and receive plain dicts.

Schema lives in migrations/ and is applied on startup, tracked in
fleet.schema_migrations.
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any
from uuid import UUID

import asyncpg

_MIGRATIONS_DIR = pathlib.Path(__file__).parent / "migrations"


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


class Database:
    """Thin asyncpg wrapper for the fleet schema."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL is required")
        self._pool = await asyncpg.create_pool(
            url, min_size=1, max_size=10, init=_init_conn
        )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    @property
    def pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("Database.connect() not called")
        return self._pool

    async def apply_migrations(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                "CREATE SCHEMA IF NOT EXISTS fleet;"
                " CREATE TABLE IF NOT EXISTS fleet.schema_migrations ("
                "   name TEXT PRIMARY KEY,"
                "   applied_at TIMESTAMPTZ NOT NULL DEFAULT now()"
                " );"
            )
            applied = {
                r["name"]
                for r in await conn.fetch("SELECT name FROM fleet.schema_migrations")
            }
            for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(path.read_text())
                    await conn.execute(
                        "INSERT INTO fleet.schema_migrations (name) VALUES ($1)",
                        path.name,
                    )

    async def upsert_agent(
        self,
        *,
        name: str,
        kind: str,
        deploy_target: str | None,
        version: str | None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO fleet.agents (name, kind, deploy_target, version, last_seen_at)
                VALUES ($1, $2, $3, $4, now())
                ON CONFLICT (name) DO UPDATE SET
                    kind = EXCLUDED.kind,
                    deploy_target = COALESCE(EXCLUDED.deploy_target, fleet.agents.deploy_target),
                    version = COALESCE(EXCLUDED.version, fleet.agents.version),
                    last_seen_at = now()
                RETURNING id, name
                """,
                name, kind, deploy_target, version,
            )
            return dict(row)

    async def heartbeat(self, *, agent_name: str, status: dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE fleet.agents
                SET last_seen_at = now(),
                    metadata = COALESCE(metadata, '{}'::jsonb) || $2
                WHERE name = $1
                """,
                agent_name, status,
            )

    async def start_run(
        self,
        *,
        agent_name: str,
        kind: str,
        intent: str | None,
        langfuse_trace_id: str | None,
        parent_run_id: str | None,
    ) -> UUID:
        parent = UUID(parent_run_id) if parent_run_id else None
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO fleet.runs
                    (agent_id, parent_run_id, kind, intent, langfuse_trace_id)
                SELECT id, $2, $3, $4, $5 FROM fleet.agents WHERE name = $1
                RETURNING id
                """,
                agent_name, parent, kind, intent, langfuse_trace_id,
            )

    async def end_run(
        self,
        *,
        run_id: str,
        status: str,
        summary: dict[str, Any],
    ) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE fleet.runs
                SET status = $2, summary = $3, finished_at = now()
                WHERE id = $1
                """,
                UUID(run_id), status, summary,
            )

    async def record_event(
        self,
        *,
        agent_name: str,
        kind: str,
        payload: dict[str, Any],
        run_id: str | None,
        level: str,
    ) -> int:
        rid = UUID(run_id) if run_id else None
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO fleet.events (agent_id, run_id, kind, payload, level)
                SELECT id, $2, $3, $4, $5 FROM fleet.agents WHERE name = $1
                RETURNING id
                """,
                agent_name, rid, kind, payload, level,
            )

    async def recent_events(
        self,
        *,
        since_minutes: int,
        agent_name: str | None,
        kind: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT e.id, e.ts, e.level, e.kind, e.payload,
                       a.name AS agent_name, e.run_id
                FROM fleet.events e
                JOIN fleet.agents a ON a.id = e.agent_id
                WHERE e.ts >= now() - make_interval(mins => $1)
                  AND ($2::text IS NULL OR a.name = $2)
                  AND ($3::text IS NULL OR e.kind = $3)
                ORDER BY e.ts DESC
                LIMIT $4
                """,
                since_minutes, agent_name, kind, limit,
            )
            return [
                {
                    "id": r["id"],
                    "ts": r["ts"].isoformat(),
                    "level": r["level"],
                    "kind": r["kind"],
                    "payload": r["payload"],
                    "agent_name": r["agent_name"],
                    "run_id": str(r["run_id"]) if r["run_id"] else None,
                }
                for r in rows
            ]

    async def signal_intent(
        self,
        *,
        target_agent: str,
        kind: str,
        payload: dict[str, Any],
        source_agent: str | None,
    ) -> UUID:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO fleet.intents
                    (target_agent_id, source_agent_id, kind, payload)
                SELECT
                    (SELECT id FROM fleet.agents WHERE name = $1),
                    (SELECT id FROM fleet.agents WHERE name = $2),
                    $3, $4
                RETURNING id
                """,
                target_agent, source_agent, kind, payload,
            )

    async def consume_intents(
        self, *, agent_name: str, limit: int
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                WITH claimed AS (
                    SELECT i.id
                    FROM fleet.intents i
                    JOIN fleet.agents a ON a.id = i.target_agent_id
                    WHERE a.name = $1 AND i.status = 'open'
                    ORDER BY i.created_at
                    LIMIT $2
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE fleet.intents i
                SET status = 'consumed', consumed_at = now()
                FROM claimed
                WHERE i.id = claimed.id
                RETURNING i.id, i.kind, i.payload, i.created_at,
                          (SELECT name FROM fleet.agents WHERE id = i.source_agent_id) AS source
                """,
                agent_name, limit,
            )
            return [
                {
                    "id": str(r["id"]),
                    "kind": r["kind"],
                    "payload": r["payload"],
                    "created_at": r["created_at"].isoformat(),
                    "source": r["source"],
                }
                for r in rows
            ]

    async def fleet_status(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    a.name, a.kind, a.deploy_target, a.version, a.last_seen_at,
                    (
                        SELECT row_to_json(r)::jsonb
                        FROM (
                            SELECT id::text AS id, kind, status,
                                   started_at, finished_at, intent
                            FROM fleet.runs
                            WHERE agent_id = a.id
                            ORDER BY started_at DESC
                            LIMIT 1
                        ) r
                    ) AS last_run,
                    (
                        SELECT count(*)::int FROM fleet.intents i
                        WHERE i.target_agent_id = a.id AND i.status = 'open'
                    ) AS open_intents
                FROM fleet.agents a
                ORDER BY a.name
                """
            )
            return [
                {
                    "name": r["name"],
                    "kind": r["kind"],
                    "deploy_target": r["deploy_target"],
                    "version": r["version"],
                    "last_seen_at": r["last_seen_at"].isoformat() if r["last_seen_at"] else None,
                    "last_run": r["last_run"],
                    "open_intents": r["open_intents"],
                }
                for r in rows
            ]
