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
        self._pool = await asyncpg.create_pool(url, min_size=1, max_size=10, init=_init_conn)

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
                r["name"] for r in await conn.fetch("SELECT name FROM fleet.schema_migrations")
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
                name,
                kind,
                deploy_target,
                version,
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
                agent_name,
                status,
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
                agent_name,
                parent,
                kind,
                intent,
                langfuse_trace_id,
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
                UUID(run_id),
                status,
                summary,
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
                agent_name,
                rid,
                kind,
                payload,
                level,
            )

    # ── Artifact content capture (motto-director output critic) ───────────
    # Real outputs (PR diffs, draft emails, scripts, comp narratives) land
    # inline in fleet.artifacts.content JSONB so the director's output_critic
    # lens can read and judge them. review_status starts at 'pending' and
    # transitions to 'passed' / 'flagged' / 'blocked' after critique.

    async def record_artifact_content(
        self,
        *,
        agent_name: str,
        kind: str,
        name: str | None,
        body: str,
        run_id: str | None,
        intent: str | None,
        repo: str | None,
        meta: dict[str, Any] | None,
        send_blocking: bool,
    ) -> int:
        """Insert into fleet.artifacts. body is stored verbatim as text inside
        the content JSONB so the critic can read it back without a separate
        fetch step. Truncates to 1MB defensively to keep Neon storage sane.
        """
        rid = UUID(run_id) if run_id else None
        # 1MB cap (Neon JSONB is fine well past this; the cap is to keep a
        # single bad agent from filling the table). Truncated bodies still
        # critique usefully — the critic sees a 'truncated' flag in meta.
        max_bytes = 1_000_000
        body_str = body or ""
        truncated = False
        if len(body_str.encode("utf-8", errors="replace")) > max_bytes:
            body_str = body_str.encode("utf-8", errors="replace")[:max_bytes].decode(
                "utf-8", errors="replace"
            )
            truncated = True
        content = {
            "body": body_str,
            "intent": intent or "",
            "repo": repo or "",
            "meta": meta or {},
            "truncated": truncated,
            "review_status": "pending",
            "send_blocking": bool(send_blocking),
        }
        async with self.pool.acquire() as conn:
            return await conn.fetchval(
                """
                INSERT INTO fleet.artifacts (run_id, agent_id, kind, name, content)
                SELECT $2, id, $3, $4, $5
                FROM fleet.agents WHERE name = $1
                RETURNING id
                """,
                agent_name,
                rid,
                kind,
                name,
                content,
            )

    async def artifacts_pending_review(
        self,
        *,
        since_hours: int = 24,
        limit: int = 25,
        agent_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recent artifacts whose content.review_status is still 'pending'.

        Returns newest-first. Director's output_critic lens calls this each
        tick to find work to review. Filters: window (since_hours), agent.
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT ar.id, ar.run_id, ar.ts, ar.kind, ar.name, ar.content,
                       a.name AS agent_name
                FROM fleet.artifacts ar
                JOIN fleet.agents a ON a.id = ar.agent_id
                WHERE ar.ts >= now() - make_interval(hours => $1)
                  AND COALESCE(ar.content->>'review_status', 'pending') = 'pending'
                  AND ($2::text IS NULL OR a.name = $2)
                ORDER BY ar.ts DESC
                LIMIT $3
                """,
                int(since_hours),
                agent_name,
                int(limit),
            )
            return [
                {
                    "id": int(r["id"]),
                    "run_id": str(r["run_id"]) if r["run_id"] else None,
                    "ts": r["ts"].isoformat(),
                    "kind": r["kind"],
                    "name": r["name"],
                    "agent_name": r["agent_name"],
                    "content": r["content"] or {},
                }
                for r in rows
            ]

    async def mark_artifact_reviewed(
        self,
        *,
        artifact_id: int,
        review_status: str,
        critique: dict[str, Any] | None = None,
    ) -> bool:
        """Update content.review_status (+ optional critique payload) on an
        artifact row. Allowed statuses: 'passed' | 'flagged' | 'blocked'.
        Uses jsonb || merge so we don't blow away the original body.
        """
        if review_status not in ("passed", "flagged", "blocked"):
            raise ValueError(f"invalid review_status: {review_status}")
        patch: dict[str, Any] = {"review_status": review_status}
        if critique is not None:
            patch["critique"] = critique
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE fleet.artifacts
                SET content = COALESCE(content, '{}'::jsonb)
                            || $2::jsonb
                            || jsonb_build_object('reviewed_at', now()::text)
                WHERE id = $1
                """,
                int(artifact_id),
                patch,
            )
            return result.endswith(" 1")

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
                since_minutes,
                agent_name,
                kind,
                limit,
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
                target_agent,
                source_agent,
                kind,
                payload,
            )

    async def consume_intents(self, *, agent_name: str, limit: int) -> list[dict[str, Any]]:
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
                agent_name,
                limit,
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
                since_minutes,
                agent_name,
                status,
                limit,
            )
            return [d for r in rows if (d := _run_row_to_dict(r)) is not None]

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
                rid,
                agent_name,
                choice,
                limit,
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
            result = await conn.execute("DELETE FROM fleet.locks WHERE resource = $1", resource)
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
                    "payload": json.loads(r["payload"])
                    if isinstance(r["payload"], str)
                    else r["payload"],
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

    async def director_claim_next_step(
        self,
        *,
        runner_id: str,
        kinds: list[str] | None = None,
        limit: int = 1,
    ) -> list[dict[str, Any]]:
        """Atomically claim up to `limit` approved pending_moves rows for this runner.

        Uses FOR UPDATE SKIP LOCKED so multiple droids polling the same queue
        do not double-claim. Returns the claimed rows in priority DESC,
        created_at ASC order (high-priority moves first, oldest within tier).
        """
        async with self.pool.acquire() as conn:
            if kinds:
                rows = await conn.fetch(
                    """
                    UPDATE public.pending_moves
                       SET status = 'claimed',
                           claimed_by = $1,
                           claimed_at = NOW(),
                           updated_at = NOW()
                     WHERE id IN (
                       SELECT id FROM public.pending_moves
                        WHERE status = 'approved'
                          AND kind = ANY($2::text[])
                        ORDER BY priority DESC, created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT $3
                     )
                    RETURNING id, run_id, created_at, updated_at, repo, kind,
                              title, rationale, intent, priority, move_payload,
                              status, approved_by, approved_at, applied_at,
                              apply_detail
                    """,
                    runner_id,
                    kinds,
                    int(limit),
                )
            else:
                rows = await conn.fetch(
                    """
                    UPDATE public.pending_moves
                       SET status = 'claimed',
                           claimed_by = $1,
                           claimed_at = NOW(),
                           updated_at = NOW()
                     WHERE id IN (
                       SELECT id FROM public.pending_moves
                        WHERE status = 'approved'
                        ORDER BY priority DESC, created_at ASC
                        FOR UPDATE SKIP LOCKED
                        LIMIT $2
                     )
                    RETURNING id, run_id, created_at, updated_at, repo, kind,
                              title, rationale, intent, priority, move_payload,
                              status, approved_by, approved_at, applied_at,
                              apply_detail
                    """,
                    runner_id,
                    int(limit),
                )
            return [_pending_row_to_dict(r) for r in rows]

    async def director_release_claim(
        self,
        *,
        move_id: int,
        runner_id: str,
        reason: str,
    ) -> bool:
        """Release a claimed move back to 'approved' so another runner can
        pick it up. Returns True iff exactly one row was updated (i.e. it
        was actually claimed by this runner)."""
        async with self.pool.acquire() as conn:
            tag = await conn.execute(
                """
                UPDATE public.pending_moves
                   SET status = 'approved',
                       claimed_by = NULL,
                       claimed_at = NULL,
                       updated_at = NOW()
                 WHERE id = $1
                   AND status = 'claimed'
                   AND claimed_by = $2
                """,
                int(move_id),
                runner_id,
            )
            # reason is logged by the caller; we don't persist it on the row
            # (no column for it) — kept in the signature so a future
            # release_reason column can be wired in without API churn.
            _ = reason
            return tag.endswith(" 1")

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

    async def director_approve_move(self, *, move_id: int, approved_by: str) -> bool:
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

    async def director_reject_move(self, *, move_id: int, approved_by: str) -> bool:
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

    async def director_bulk_approve(self, *, move_ids: list[int], approved_by: str) -> int:
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

    # ── Director epics ───────────────────────────────────────────────────
    # epics table is owned by motto-director migrations (0006_epics.sql).
    # Cockpit reads/writes the same rows so a human can approve, pause, or
    # abandon a multi-cycle plan before director keeps spending on it.

    async def director_epics(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """List epics, optionally filtered by status. status='all' or None
        returns every status. Includes a count of related pending_moves
        keyed by epic_id (via run_id match) so the UI can show progress."""
        async with self.pool.acquire() as conn:
            if status and status != "all":
                rows = await conn.fetch(
                    """
                    SELECT id, run_id, title, kpi_ref, rationale,
                           estimated_cycles, success_criteria, plan,
                           status, approved_by, approved_at,
                           closed_at, closed_reason,
                           created_at, updated_at
                    FROM public.epics
                    WHERE status = $1
                    ORDER BY created_at DESC
                    LIMIT $2
                    """,
                    status,
                    int(limit),
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, run_id, title, kpi_ref, rationale,
                           estimated_cycles, success_criteria, plan,
                           status, approved_by, approved_at,
                           closed_at, closed_reason,
                           created_at, updated_at
                    FROM public.epics
                    ORDER BY created_at DESC
                    LIMIT $1
                    """,
                    int(limit),
                )
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                # Plan is stored as jsonb — asyncpg gives back a dict already,
                # but if it's a str (older rows), parse it.
                plan = d.get("plan")
                if isinstance(plan, str):
                    try:
                        d["plan"] = json.loads(plan)
                    except (ValueError, TypeError):
                        d["plan"] = []
                # ISO-ize timestamps
                for k in (
                    "created_at",
                    "updated_at",
                    "approved_at",
                    "closed_at",
                ):
                    v = d.get(k)
                    if v is not None:
                        d[k] = v.isoformat()
                out.append(d)
            return out

    async def director_epic_counts(self) -> dict[str, int]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT status, count(*)::int AS n
                FROM public.epics
                GROUP BY status
                """
            )
            return {r["status"]: r["n"] for r in rows}

    async def director_set_epic_status(
        self,
        *,
        epic_id: int,
        new_status: str,
        approved_by: str,
        closed_reason: str = "",
    ) -> bool:
        """Transition an epic to a new status. Allowed:
        proposed -> active | abandoned
        active   -> paused | closed | abandoned
        paused   -> active | abandoned
        """
        allowed = {"proposed", "active", "paused", "closed", "abandoned"}
        if new_status not in allowed:
            return False
        async with self.pool.acquire() as conn:
            if new_status in ("closed", "abandoned"):
                result = await conn.execute(
                    """
                    UPDATE public.epics
                    SET status = $2,
                        approved_by = COALESCE(approved_by, $3),
                        closed_at = NOW(),
                        closed_reason = $4,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    int(epic_id),
                    new_status,
                    approved_by,
                    closed_reason,
                )
            elif new_status == "active":
                result = await conn.execute(
                    """
                    UPDATE public.epics
                    SET status = 'active',
                        approved_by = $2,
                        approved_at = COALESCE(approved_at, NOW()),
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    int(epic_id),
                    approved_by,
                )
            else:  # paused or proposed
                result = await conn.execute(
                    """
                    UPDATE public.epics
                    SET status = $2, updated_at = NOW()
                    WHERE id = $1
                    """,
                    int(epic_id),
                    new_status,
                )
            return result.endswith(" 1")

    # ── Planner observability ────────────────────────────────────────────

    async def latest_planner_event(self) -> dict[str, Any] | None:
        """Return the most recent fleet.events row of kind='planner.cycle',
        used by the cockpit to show what DeepSeek's planner most recently
        produced (parsed/inserted/output preview/finish_reason/tokens)."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, ts, agent_id, kind, run_id, payload
                FROM fleet.events
                WHERE kind = 'planner.cycle'
                ORDER BY id DESC
                LIMIT 1
                """
            )
            if not row:
                return None
            d = dict(row)
            v = d.get("ts")
            if v is not None:
                d["ts"] = v.isoformat()
            payload = d.get("payload")
            if isinstance(payload, str):
                try:
                    d["payload"] = json.loads(payload)
                except (ValueError, TypeError):
                    d["payload"] = {}
            return d

    # ── Verify_move framework ────────────────────────────────────────────
    # Tables owned by 0005_verify_and_capabilities.sql. Three concerns:
    #   - move_verifications  : outcomes of verifier runs
    #   - capability_requests : director-asks-human for resources
    #   - trust_scores        : rolling per-scope confidence

    async def enqueue_pending_move(
        self,
        *,
        run_id: str,
        repo: str,
        kind: str,
        title: str,
        move_payload: dict[str, Any],
        rationale: str = "",
        intent: str = "",
        priority: int = 0,
    ) -> dict[str, Any]:
        """Insert a row into public.pending_moves. Used by chat-initiated
        moves so they flow through the same approval queue motto-director uses.

        Returns the inserted row (id, status='pending', etc.) so the caller
        can echo it back to the chat UI for approval.
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO public.pending_moves
                  (run_id, repo, kind, title, rationale, intent, priority,
                   move_payload, status)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, 'pending')
                RETURNING id, run_id, created_at, repo, kind, title,
                          rationale, intent, priority, move_payload, status
                """,
                str(run_id),
                str(repo),
                str(kind),
                str(title),
                str(rationale or ""),
                str(intent or ""),
                int(priority),
                json.dumps(move_payload or {}),
            )
            d = dict(row)
            if d.get("created_at") is not None:
                d["created_at"] = d["created_at"].isoformat()
            mp = d.get("move_payload")
            if isinstance(mp, str):
                try:
                    d["move_payload"] = json.loads(mp)
                except json.JSONDecodeError:
                    pass
            return d

    async def fetch_pending_move(self, move_id: int) -> dict[str, Any] | None:
        """Read a single pending_moves row by id. The table lives in `public`
        and is owned by motto-director migrations; we just read it."""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, run_id, created_at, updated_at, repo, kind, title,
                       rationale, intent, priority, move_payload, status,
                       approved_by, approved_at, applied_at, apply_detail
                FROM public.pending_moves
                WHERE id = $1
                """,
                int(move_id),
            )
            if not row:
                return None
            d = dict(row)
            for k in ("created_at", "updated_at", "approved_at", "applied_at"):
                v = d.get(k)
                if v is not None:
                    d[k] = v.isoformat()
            mp = d.get("move_payload")
            if isinstance(mp, str):
                try:
                    d["move_payload"] = json.loads(mp)
                except (ValueError, TypeError):
                    d["move_payload"] = {}
            return d

    async def record_verification(
        self,
        *,
        move_id: int,
        repo: str,
        kind: str,
        verifier: str,
        status: str,
        evidence: dict[str, Any] | None = None,
        kpi_delta: dict[str, Any] | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO fleet.move_verifications
                    (move_id, repo, kind, verifier, status, evidence,
                     kpi_delta, error, completed_at, duration_ms, requested_by)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, NOW(), $9, $10)
                RETURNING id, started_at, completed_at
                """,
                int(move_id),
                repo,
                kind,
                verifier,
                status,
                json.dumps(evidence or {}),
                json.dumps(kpi_delta or {}),
                error,
                duration_ms,
                requested_by,
            )
            d = dict(row)
            for k in ("started_at", "completed_at"):
                v = d.get(k)
                if v is not None:
                    d[k] = v.isoformat()
            return d

    async def list_verifications(
        self,
        *,
        move_id: int | None = None,
        repo: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        i = 1
        if move_id is not None:
            clauses.append(f"move_id = ${i}")
            params.append(int(move_id))
            i += 1
        if repo:
            clauses.append(f"repo = ${i}")
            params.append(repo)
            i += 1
        if status:
            clauses.append(f"status = ${i}")
            params.append(status)
            i += 1
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT id, move_id, repo, kind, verifier, status, evidence,"
            " kpi_delta, error, started_at, completed_at, duration_ms,"
            " requested_by FROM fleet.move_verifications"
            f"{where} ORDER BY id DESC LIMIT ${i}"
        )
        params.append(int(limit))
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                for k in ("started_at", "completed_at"):
                    v = d.get(k)
                    if v is not None:
                        d[k] = v.isoformat()
                for k in ("evidence", "kpi_delta"):
                    v = d.get(k)
                    if isinstance(v, str):
                        try:
                            d[k] = json.loads(v)
                        except (ValueError, TypeError):
                            d[k] = {}
                out.append(d)
            return out

    # ── Capability requests ──────────────────────────────────────────────

    async def file_capability_request(
        self,
        *,
        capability: str,
        justification: str,
        requested_by: str,
        repo: str | None = None,
        move_id: int | None = None,
    ) -> dict[str, Any]:
        """Idempotent on (capability, status='pending'): if a pending
        request for this capability already exists, return it instead
        of opening a duplicate."""
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id, capability, repo, move_id, justification,
                       status, requested_by, requested_at
                FROM fleet.capability_requests
                WHERE capability = $1 AND status = 'pending'
                ORDER BY id DESC LIMIT 1
                """,
                capability,
            )
            if existing:
                d = dict(existing)
                v = d.get("requested_at")
                if v is not None:
                    d["requested_at"] = v.isoformat()
                d["already_pending"] = True
                return d
            row = await conn.fetchrow(
                """
                INSERT INTO fleet.capability_requests
                    (capability, repo, move_id, justification, requested_by)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING id, capability, repo, move_id, justification,
                          status, requested_by, requested_at
                """,
                capability,
                repo,
                move_id,
                justification,
                requested_by,
            )
            d = dict(row)
            v = d.get("requested_at")
            if v is not None:
                d["requested_at"] = v.isoformat()
            d["already_pending"] = False
            return d

    async def list_capability_requests(
        self, *, status: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            if status and status != "all":
                rows = await conn.fetch(
                    """
                    SELECT id, capability, repo, move_id, justification,
                           status, requested_by, requested_at,
                           decided_by, decided_at, grant_detail, deny_reason
                    FROM fleet.capability_requests
                    WHERE status = $1
                    ORDER BY requested_at DESC LIMIT $2
                    """,
                    status,
                    int(limit),
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, capability, repo, move_id, justification,
                           status, requested_by, requested_at,
                           decided_by, decided_at, grant_detail, deny_reason
                    FROM fleet.capability_requests
                    ORDER BY requested_at DESC LIMIT $1
                    """,
                    int(limit),
                )
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                for k in ("requested_at", "decided_at"):
                    v = d.get(k)
                    if v is not None:
                        d[k] = v.isoformat()
                out.append(d)
            return out

    async def decide_capability_request(
        self,
        *,
        request_id: int,
        decision: str,  # 'granted' | 'denied'
        decided_by: str,
        grant_detail: str | None = None,
        deny_reason: str | None = None,
    ) -> bool:
        if decision not in ("granted", "denied"):
            return False
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE fleet.capability_requests
                SET status = $2, decided_by = $3, decided_at = NOW(),
                    grant_detail = $4, deny_reason = $5
                WHERE id = $1 AND status = 'pending'
                """,
                int(request_id),
                decision,
                decided_by,
                grant_detail,
                deny_reason,
            )
            return result.endswith("UPDATE 1")

    # ── Trust scores ─────────────────────────────────────────────────────

    async def update_trust_score(
        self, *, scope: str, passed: bool, alpha: float = 0.2
    ) -> dict[str, Any]:
        """EWMA-style update: new = (1-α)*old + α*sample. sample=1 if passed,
        else 0. First sample on a brand-new scope starts at 0.5 prior."""
        sample = 1.0 if passed else 0.0
        async with self.pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT score, sample_size FROM fleet.trust_scores WHERE scope = $1",
                scope,
            )
            if existing:
                old = float(existing["score"])
                new = (1.0 - alpha) * old + alpha * sample
                row = await conn.fetchrow(
                    """
                    UPDATE fleet.trust_scores
                    SET score = $2, sample_size = sample_size + 1,
                        last_passed_at = CASE WHEN $3 THEN NOW() ELSE last_passed_at END,
                        last_failed_at = CASE WHEN NOT $3 THEN NOW() ELSE last_failed_at END,
                        updated_at = NOW()
                    WHERE scope = $1
                    RETURNING scope, score, sample_size,
                              last_passed_at, last_failed_at, updated_at
                    """,
                    scope,
                    new,
                    passed,
                )
            else:
                # First sample — blend 0.5 prior with the observation.
                new = 0.5 * 0.5 + 0.5 * sample
                row = await conn.fetchrow(
                    """
                    INSERT INTO fleet.trust_scores
                        (scope, score, sample_size,
                         last_passed_at, last_failed_at)
                    VALUES (
                        $1, $2, 1,
                        CASE WHEN $3 THEN NOW() ELSE NULL END,
                        CASE WHEN NOT $3 THEN NOW() ELSE NULL END
                    )
                    RETURNING scope, score, sample_size,
                              last_passed_at, last_failed_at, updated_at
                    """,
                    scope,
                    new,
                    passed,
                )
            d = dict(row)
            for k in ("last_passed_at", "last_failed_at", "updated_at"):
                v = d.get(k)
                if v is not None:
                    d[k] = v.isoformat()
            return d

    async def get_trust_scores(self, *, scope: str | None = None) -> list[dict[str, Any]]:
        async with self.pool.acquire() as conn:
            if scope:
                rows = await conn.fetch(
                    "SELECT scope, score, sample_size, last_passed_at,"
                    " last_failed_at, updated_at FROM fleet.trust_scores"
                    " WHERE scope = $1",
                    scope,
                )
            else:
                rows = await conn.fetch(
                    "SELECT scope, score, sample_size, last_passed_at,"
                    " last_failed_at, updated_at FROM fleet.trust_scores"
                    " ORDER BY scope"
                )
            out: list[dict[str, Any]] = []
            for r in rows:
                d = dict(r)
                for k in ("last_passed_at", "last_failed_at", "updated_at"):
                    v = d.get(k)
                    if v is not None:
                        d[k] = v.isoformat()
                out.append(d)
            return out
