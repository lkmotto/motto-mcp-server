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


def _run_row_to_dict(r: asyncpg.Record | None) -> dict[str, Any] | None:
    if r is None:
        return None
    return {
        "id": str(r["id"]),
        "agent_name": r["agent_name"],
        "kind": r["kind"],
        "intent": r["intent"],
        "status": r["status"],
        "langfuse_trace_id": r["langfuse_trace_id"],
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
        "summary": r["summary"],
        "parent_run_id": str(r["parent_run_id"]) if r["parent_run_id"] else None,
    }


def _event_row_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"].isoformat(),
        "level": r["level"],
        "kind": r["kind"],
        "payload": r["payload"],
        "agent_name": r["agent_name"],
        "run_id": str(r["run_id"]) if r["run_id"] else None,
    }


def _decision_row_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"].isoformat(),
        "choice": r["choice"],
        "rationale": r["rationale"],
        "payload": r["payload"],
        "agent_name": r["agent_name"],
        "run_id": str(r["run_id"]) if r["run_id"] else None,
    }


def _pending_row_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    payload = r["move_payload"]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            payload = {}
    return {
        "id": int(r["id"]),
        "run_id": r["run_id"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
        "repo": r["repo"],
        "kind": r["kind"],
        "title": r["title"],
        "rationale": r["rationale"] or "",
        "intent": r["intent"] or "",
        "priority": int(r["priority"] or 0),
        "move_payload": payload,
        "status": r["status"],
        "approved_by": r["approved_by"],
        "approved_at": r["approved_at"].isoformat() if r["approved_at"] else None,
        "applied_at": r["applied_at"].isoformat() if r["applied_at"] else None,
        "apply_detail": r["apply_detail"] or "",
    }


def _artifact_row_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "id": r["id"],
        "ts": r["ts"].isoformat(),
        "kind": r["kind"],
        "name": r["name"],
        "content": r["content"],
        "agent_name": r["agent_name"],
        "run_id": str(r["run_id"]) if r["run_id"] else None,
    }


class Database:
    """Thin asyncpg wrapper for the fleet schema."""

    def __init__(self) -> None:
        self._pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            raise RuntimeError("DATABASE_URL (or NEON_DATABASE_URL) is required")
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

    async def list_runs(
        self,
        *,
        agent_name: str | None,
        status: str | None,
        since_minutes: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT r.id, r.kind, r.intent, r.status, r.langfuse_trace_id,
                       r.started_at, r.finished_at, r.summary,
                       r.parent_run_id, a.name AS agent_name
                FROM fleet.runs r
                JOIN fleet.agents a ON a.id = r.agent_id
                WHERE r.started_at >= now() - make_interval(mins => $1)
                  AND ($2::text IS NULL OR a.name = $2)
                  AND ($3::text IS NULL OR r.status = $3)
                ORDER BY r.started_at DESC
                LIMIT $4
                """,
                since_minutes, agent_name, status, limit,
            )
            return [_run_row_to_dict(r) for r in rows]

    async def get_run(self, *, run_id: str) -> dict[str, Any]:
        rid = UUID(run_id)
        async with self.pool.acquire() as conn:
            run_row = await conn.fetchrow(
                """
                SELECT r.id, r.kind, r.intent, r.status, r.langfuse_trace_id,
                       r.started_at, r.finished_at, r.summary,
                       r.parent_run_id, a.name AS agent_name
                FROM fleet.runs r
                JOIN fleet.agents a ON a.id = r.agent_id
                WHERE r.id = $1
                """,
                rid,
            )
            event_rows = await conn.fetch(
                """
                SELECT e.id, e.ts, e.level, e.kind, e.payload,
                       a.name AS agent_name, e.run_id
                FROM fleet.events e
                JOIN fleet.agents a ON a.id = e.agent_id
                WHERE e.run_id = $1
                ORDER BY e.ts ASC
                """,
                rid,
            )
            decision_rows = await conn.fetch(
                """
                SELECT d.id, d.ts, d.choice, d.rationale, d.payload,
                       a.name AS agent_name, d.run_id
                FROM fleet.decisions d
                JOIN fleet.agents a ON a.id = d.agent_id
                WHERE d.run_id = $1
                ORDER BY d.ts ASC
                """,
                rid,
            )
            artifact_rows = await conn.fetch(
                """
                SELECT ar.id, ar.ts, ar.kind, ar.name, ar.content, ar.run_id,
                       a.name AS agent_name
                FROM fleet.artifacts ar
                LEFT JOIN fleet.agents a ON a.id = ar.agent_id
                WHERE ar.run_id = $1
                ORDER BY ar.ts ASC
                """,
                rid,
            )
        return {
            "run": _run_row_to_dict(run_row),
            "events": [_event_row_to_dict(r) for r in event_rows],
            "decisions": [_decision_row_to_dict(r) for r in decision_rows],
            "artifacts": [_artifact_row_to_dict(r) for r in artifact_rows],
        }

    async def get_decisions(
        self,
        *,
        run_id: str | None,
        agent_name: str | None,
        choice: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        rid = UUID(run_id) if run_id else None
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT d.id, d.ts, d.choice, d.rationale, d.payload,
                       a.name AS agent_name, d.run_id
                FROM fleet.decisions d
                JOIN fleet.agents a ON a.id = d.agent_id
                WHERE ($1::uuid IS NULL OR d.run_id = $1)
                  AND ($2::text IS NULL OR a.name = $2)
                  AND ($3::text IS NULL OR d.choice = $3)
                ORDER BY d.ts DESC
                LIMIT $4
                """,
                rid, agent_name, choice, limit,
            )
            return [_decision_row_to_dict(r) for r in rows]

    async def get_locks(self) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT resource, holder_run, acquired_at, expires_at
                FROM fleet.locks
                WHERE expires_at > now()
                ORDER BY acquired_at DESC
                """
            )
            return [
                {
                    "resource": r["resource"],
                    "holder_run": str(r["holder_run"]) if r["holder_run"] else None,
                    "acquired_at": r["acquired_at"].isoformat() if r["acquired_at"] else None,
                    "expires_at": r["expires_at"].isoformat() if r["expires_at"] else None,
                }
                for r in rows
            ]

    async def force_release_lock(self, *, resource: str) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM fleet.locks WHERE resource = $1", resource
            )
        # asyncpg execute() returns 'DELETE <n>' for DELETE statements.
        try:
            n = int(result.split()[-1])
        except (ValueError, IndexError):
            n = 0
        return n > 0

    async def replay_run(self, *, run_id: str) -> dict[str, Any]:
        rid = UUID(run_id)
        base = await self.get_run(run_id=run_id)
        parent: dict[str, Any] | None = None
        async with self.pool.acquire() as conn:
            if base["run"] and base["run"].get("parent_run_id"):
                parent_row = await conn.fetchrow(
                    """
                    SELECT r.id, r.kind, r.intent, r.status, r.langfuse_trace_id,
                           r.started_at, r.finished_at, r.summary,
                           r.parent_run_id, a.name AS agent_name
                    FROM fleet.runs r
                    JOIN fleet.agents a ON a.id = r.agent_id
                    WHERE r.id = $1
                    """,
                    UUID(base["run"]["parent_run_id"]),
                )
                parent = _run_row_to_dict(parent_row)
            child_rows = await conn.fetch(
                """
                SELECT r.id, r.kind, r.intent, r.status, r.langfuse_trace_id,
                       r.started_at, r.finished_at, r.summary,
                       r.parent_run_id, a.name AS agent_name
                FROM fleet.runs r
                JOIN fleet.agents a ON a.id = r.agent_id
                WHERE r.parent_run_id = $1
                ORDER BY r.started_at ASC
                """,
                rid,
            )
        return {
            **base,
            "parent_run": parent,
            "child_runs": [_run_row_to_dict(r) for r in child_rows],
        }

    # ── local-task queue ────────────────────────────────────────────────────

    async def queue_local_task(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        source: str = "cockpit-user",
        description: str | None = None,
        dedup_key: str | None = None,
        ttl_seconds: int = 600,
    ) -> dict[str, Any]:
        """Insert a task for the local runner. Returns row."""
        async with self.pool.acquire() as conn:
            # Dedup: if a queued/claimed task with same dedup_key exists, return it
            if dedup_key:
                existing = await conn.fetchrow(
                    """
                    SELECT id::text AS id, status FROM fleet.local_tasks
                    WHERE dedup_key = $1
                      AND status IN ('queued','claimed','running')
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    dedup_key,
                )
                if existing:
                    return {"id": existing["id"], "status": existing["status"], "dedup_hit": True}
            row = await conn.fetchrow(
                """
                INSERT INTO fleet.local_tasks
                  (kind, payload, source, description, dedup_key, ttl_seconds)
                VALUES ($1, $2::jsonb, $3, $4, $5, $6)
                RETURNING id::text AS id, status, created_at
                """,
                kind,
                json.dumps(payload),
                source,
                description,
                dedup_key,
                ttl_seconds,
            )
            return {
                "id": row["id"],
                "status": row["status"],
                "created_at": row["created_at"].isoformat(),
                "dedup_hit": False,
            }

    async def claim_local_tasks(
        self,
        *,
        runner_id: str,
        kinds: list[str] | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Atomically claim queued tasks for this runner. Filters by kinds if given."""
        async with self.pool.acquire() as conn:
            # Sweep expired before claiming so the runner doesn't pick up stale work
            await conn.execute("SELECT fleet.expire_local_tasks()")
            if kinds:
                rows = await conn.fetch(
                    """
                    UPDATE fleet.local_tasks
                       SET status = 'claimed', claimed_at = now(), claimed_by = $1
                     WHERE id IN (
                       SELECT id FROM fleet.local_tasks
                        WHERE status = 'queued' AND kind = ANY($2::text[])
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT $3
                     )
                    RETURNING id::text AS id, kind, payload, source,
                              description, created_at
                    """,
                    runner_id,
                    kinds,
                    limit,
                )
            else:
                rows = await conn.fetch(
                    """
                    UPDATE fleet.local_tasks
                       SET status = 'claimed', claimed_at = now(), claimed_by = $1
                     WHERE id IN (
                       SELECT id FROM fleet.local_tasks
                        WHERE status = 'queued'
                        ORDER BY created_at
                        FOR UPDATE SKIP LOCKED
                        LIMIT $2
                     )
                    RETURNING id::text AS id, kind, payload, source,
                              description, created_at
                    """,
                    runner_id,
                    limit,
                )
            return [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "payload": json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"],
                    "source": r["source"],
                    "description": r["description"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ]

    async def complete_local_task(
        self,
        *,
        task_id: str,
        status: str,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        """Mark task succeeded/failed/cancelled with result."""
        if status not in ("succeeded", "failed", "cancelled"):
            raise ValueError("status must be succeeded|failed|cancelled")
        async with self.pool.acquire() as conn:
            tag = await conn.execute(
                """
                UPDATE fleet.local_tasks
                   SET status = $2, finished_at = now(),
                       result = $3::jsonb, error = $4
                 WHERE id = $1::uuid
                   AND status IN ('claimed','running')
                """,
                task_id,
                status,
                json.dumps(result) if result is not None else None,
                error,
            )
            return tag.endswith("1")

    async def get_local_task(self, *, task_id: str) -> dict[str, Any] | None:
        async with self.pool.acquire() as conn:
            r = await conn.fetchrow(
                """
                SELECT id::text AS id, kind, payload, source, status,
                       description, dedup_key, claimed_at, claimed_by,
                       started_at, finished_at, result, error,
                       ttl_seconds, created_at
                FROM fleet.local_tasks
                WHERE id = $1::uuid
                """,
                task_id,
            )
            if not r:
                return None
            payload = r["payload"]
            if isinstance(payload, str):
                payload = json.loads(payload)
            result = r["result"]
            if isinstance(result, str):
                result = json.loads(result)
            return {
                "id": r["id"],
                "kind": r["kind"],
                "payload": payload,
                "source": r["source"],
                "status": r["status"],
                "description": r["description"],
                "dedup_key": r["dedup_key"],
                "claimed_at": r["claimed_at"].isoformat() if r["claimed_at"] else None,
                "claimed_by": r["claimed_by"],
                "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                "result": result,
                "error": r["error"],
                "ttl_seconds": r["ttl_seconds"],
                "created_at": r["created_at"].isoformat(),
            }

    async def list_local_tasks(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id::text AS id, kind, status, source, description,
                       dedup_key, claimed_by, created_at, finished_at,
                       error,
                       (result IS NOT NULL) AS has_result
                FROM fleet.local_tasks
                WHERE ($1::text IS NULL OR status = $1)
                  AND ($2::text IS NULL OR kind = $2)
                ORDER BY created_at DESC
                LIMIT $3
                """,
                status,
                kind,
                limit,
            )
            return [
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "status": r["status"],
                    "source": r["source"],
                    "description": r["description"],
                    "dedup_key": r["dedup_key"],
                    "claimed_by": r["claimed_by"],
                    "created_at": r["created_at"].isoformat(),
                    "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                    "error": r["error"],
                    "has_result": r["has_result"],
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

    # ── Director pending moves (motto-director PR #41) ───────────────────
    # The pending_moves table is owned by motto-director's migrations
    # (0005_pending_moves.sql, public schema). The cockpit reads/writes the
    # same rows so humans can approve or reject before director acts.

    async def director_pending_moves(
        self,
        *,
        status: str = "pending",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, run_id, created_at, updated_at, repo, kind,
                       title, rationale, intent, priority, move_payload,
                       status, approved_by, approved_at, applied_at,
                       apply_detail
                FROM public.pending_moves
                WHERE status = $1
                ORDER BY priority DESC, created_at DESC
                LIMIT $2
                """,
                status,
                int(limit),
            )
            return [_pending_row_to_dict(r) for r in rows]

    async def director_pending_counts(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT status, count(*)::int AS n
                FROM public.pending_moves
                GROUP BY status
                """
            )
            return {r["status"]: r["n"] for r in rows}

    async def director_approve_move(
        self, *, move_id: int, approved_by: str
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE public.pending_moves
                SET status = 'approved',
                    approved_by = $2,
                    approved_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1 AND status = 'pending'
                """,
                int(move_id),
                approved_by,
            )
            # asyncpg returns 'UPDATE n'
            return result.endswith(" 1")

    async def director_reject_move(
        self, *, move_id: int, approved_by: str
    ) -> bool:
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE public.pending_moves
                SET status = 'rejected',
                    approved_by = $2,
                    approved_at = NOW(),
                    updated_at = NOW()
                WHERE id = $1 AND status = 'pending'
                """,
                int(move_id),
                approved_by,
            )
            return result.endswith(" 1")

    async def director_bulk_approve(
        self, *, move_ids: list[int], approved_by: str
    ) -> int:
        if not move_ids:
            return 0
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE public.pending_moves
                SET status = 'approved',
                    approved_by = $2,
                    approved_at = NOW(),
                    updated_at = NOW()
                WHERE id = ANY($1::bigint[]) AND status = 'pending'
                """,
                [int(i) for i in move_ids],
                approved_by,
            )
            # 'UPDATE n'
            try:
                return int(result.split()[-1])
            except (ValueError, IndexError):
                return 0
