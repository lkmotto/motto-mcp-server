"""Grabber MCP server — rotation job queue for the Motto credential rotator.

Wraps ``fleet.grabber_jobs`` and ``fleet.grabber_playbooks`` in Neon Postgres.
All tools require ``DATABASE_URL`` to be set; they raise a clear RuntimeError
if the DB is not configured so callers get an actionable message rather than
an opaque crash.

Run with ``python -m servers.grabber`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.

Auth: ``DATABASE_URL`` from env (canonical home: Doppler ``motto-core/prd``).
The connection is established lazily on first tool call.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import asyncpg
from fastmcp.server import FastMCP

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DB helpers — thin asyncpg pool, lazy-initialised
# ---------------------------------------------------------------------------

_pool_holder: dict[str, asyncpg.Pool] = {}


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def _pool() -> asyncpg.Pool:
    if "p" not in _pool_holder:
        url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is required. Set it via Doppler (motto-core/prd) "
                "and inject at runtime. Without it the grabber MCP server cannot "
                "read or write rotation jobs."
            )
        _pool_holder["p"] = await asyncpg.create_pool(url, min_size=1, max_size=5, init=_init_conn)
    return _pool_holder["p"]


def set_pool(pool: asyncpg.Pool | None) -> None:
    """Swap the lazy pool (tests). Pass None to reset."""
    if pool is None:
        _pool_holder.pop("p", None)
    else:
        _pool_holder["p"] = pool


def _require_db() -> None:
    """Raise early if DATABASE_URL is absent (no pool yet, no env var)."""
    has_url = os.environ.get("DATABASE_URL") or os.environ.get("NEON_DATABASE_URL")
    if "p" not in _pool_holder and not has_url:
        raise RuntimeError(
            "DATABASE_URL is not configured. "
            "Set it via Doppler (motto-core/prd). "
            "The grabber MCP server cannot operate without a Neon connection."
        )


# ---------------------------------------------------------------------------
# Row serialisers
# ---------------------------------------------------------------------------


def _job_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    """Safe serialisation — NEVER includes payload / credential fields."""
    return {
        "job_id": str(r["id"]),
        "service": r["service"],
        "reason": r["reason"],
        "requested_by": r["requested_by"],
        "status": r["status"],
        "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        "started_at": r["started_at"].isoformat() if r["started_at"] else None,
        "ended_at": r["ended_at"].isoformat() if r["ended_at"] else None,
        "error_class": r["error_class"],
        "audit_decision_id": str(r["audit_decision_id"]) if r["audit_decision_id"] else None,
    }


def _job_to_dict_with_duration(r: asyncpg.Record) -> dict[str, Any]:
    base = _job_to_dict(r)
    # duration_ms: ms between started_at and ended_at when both are set
    if r["started_at"] and r["ended_at"]:
        delta = r["ended_at"] - r["started_at"]
        base["duration_ms"] = int(delta.total_seconds() * 1000)
    else:
        base["duration_ms"] = None
    return base


def _playbook_to_dict(r: asyncpg.Record) -> dict[str, Any]:
    return {
        "service": r["service"],
        "dashboard_url": r["dashboard_url"],
        "target_doppler_keys": list(r["target_doppler_keys"]),
        "last_validated_at": (
            r["last_validated_at"].isoformat() if r["last_validated_at"] else None
        ),
        "status": r["status"],
    }


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------


mcp = FastMCP(
    "motto-grabber",
    instructions=(
        "Credential-rotation job queue for the Motto fleet. "
        "Use enqueue_rotation to schedule a rotation, list_rotations to monitor "
        "progress, get_rotation for a single job's full record, cancel_rotation to "
        "abort a pending job, list_playbooks to see which services are configured, "
        "and grabber_health for a quick cluster-health digest. "
        "DATABASE_URL must be set (Doppler motto-core/prd). "
        "Credential values are NEVER returned by any tool."
    ),
)


# ---------------------------------------------------------------------------
# Tool 1 — enqueue_rotation
# ---------------------------------------------------------------------------


@mcp.tool()
async def enqueue_rotation(
    service: str,
    reason: str,
    requested_by: str = "mcp",
) -> dict[str, Any]:
    """Enqueue a credential-rotation job for a whitelisted service.

    Args:
        service: Service name (must exist in fleet.grabber_playbooks and be live
            or placeholder). Currently supported: ``anthropic``.
        reason: Human-readable reason for the rotation (e.g. "scheduled weekly",
            "suspected leak").
        requested_by: Identity of the caller; defaults to ``"mcp"``.

    Returns:
        ``{job_id, status}`` — the UUID of the new job and ``"pending"``.

    Raises:
        RuntimeError: if ``service`` is not in the playbook whitelist, or if
            DATABASE_URL is not configured.
    """
    _require_db()
    pool = await _pool()

    async with pool.acquire() as conn:
        # Validate service against the playbook whitelist (admin-managed table).
        playbook_row = await conn.fetchrow(
            "SELECT service, status FROM fleet.grabber_playbooks WHERE service = $1",
            service,
        )
        if playbook_row is None:
            raise RuntimeError(
                f"Unknown service '{service}'. "
                "Add it to fleet.grabber_playbooks before enqueueing. "
                "Currently registered services: "
                + str(
                    [
                        r["service"]
                        async for r in conn.cursor(
                            "SELECT service FROM fleet.grabber_playbooks ORDER BY service"
                        )
                    ]
                )
            )
        if playbook_row["status"] == "disabled":
            raise RuntimeError(
                f"Service '{service}' is disabled in fleet.grabber_playbooks. "
                "Set status='live' or 'placeholder' to allow rotations."
            )

        row = await conn.fetchrow(
            """
            INSERT INTO fleet.grabber_jobs (service, reason, requested_by, status)
            VALUES ($1, $2, $3, 'pending')
            RETURNING id, status
            """,
            service,
            reason,
            requested_by,
        )

    logger.info(
        "grabber.enqueue_rotation: job_id=%s service=%s requested_by=%s",
        row["id"],
        service,
        requested_by,
    )
    return {"job_id": str(row["id"]), "status": row["status"]}


# ---------------------------------------------------------------------------
# Tool 2 — list_rotations
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_rotations(
    limit: int = 20,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List rotation jobs, newest first.

    Args:
        limit: Maximum number of rows to return (default 20, max 200).
        status: Optional filter — one of ``pending``, ``running``,
            ``succeeded``, ``failed``, ``cancelled``.

    Returns:
        List of rotation records. NEVER includes payload or credential values.
    """
    _require_db()
    pool = await _pool()
    limit = min(limit, 200)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, service, reason, requested_by, status,
                   created_at, started_at, ended_at, error_class, audit_decision_id
            FROM fleet.grabber_jobs
            WHERE ($1::text IS NULL OR status = $1)
            ORDER BY created_at DESC
            LIMIT $2
            """,
            status,
            limit,
        )
    return [_job_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tool 3 — get_rotation
# ---------------------------------------------------------------------------


@mcp.tool()
async def get_rotation(job_id: str) -> dict[str, Any] | None:
    """Fetch a single rotation job by UUID.

    Args:
        job_id: UUID of the job (from enqueue_rotation or list_rotations).

    Returns:
        Full job record with ``duration_ms`` and ``audit_decision_id``, or
        ``None`` if not found. NEVER includes payload or credential values.
    """
    _require_db()
    pool = await _pool()

    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                SELECT id, service, reason, requested_by, status,
                       created_at, started_at, ended_at, error_class, audit_decision_id
                FROM fleet.grabber_jobs
                WHERE id = $1::uuid
                """,
                job_id,
            )
        except Exception as exc:
            raise RuntimeError(f"Invalid job_id '{job_id}': {exc}") from exc

    if row is None:
        return None
    return _job_to_dict_with_duration(row)


# ---------------------------------------------------------------------------
# Tool 4 — list_playbooks
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_playbooks() -> list[dict[str, Any]]:
    """List all configured playbooks from fleet.grabber_playbooks.

    Returns:
        List of playbook records: service, dashboard_url, target_doppler_keys
        (key names only — never values), last_validated_at, status.
        Status is one of: ``live``, ``placeholder``, ``disabled``.
    """
    _require_db()
    pool = await _pool()

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT service, dashboard_url, target_doppler_keys,
                   last_validated_at, status
            FROM fleet.grabber_playbooks
            ORDER BY service
            """
        )
    return [_playbook_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tool 5 — cancel_rotation
# ---------------------------------------------------------------------------


@mcp.tool()
async def cancel_rotation(job_id: str, reason: str) -> dict[str, Any]:
    """Cancel a pending rotation job. No-op if the job is running or done.

    Idempotent — safe to call multiple times.

    Args:
        job_id: UUID of the job to cancel.
        reason: Why the job is being cancelled (logged for audit).

    Returns:
        ``{"ok": True, "cancelled": True}`` if the job was pending and is now
        cancelled, or ``{"ok": True, "cancelled": False}`` if it was already
        running/done.
    """
    _require_db()
    pool = await _pool()

    async with pool.acquire() as conn:
        try:
            result = await conn.execute(
                """
                UPDATE fleet.grabber_jobs
                SET status = 'cancelled'
                WHERE id = $1::uuid AND status = 'pending'
                """,
                job_id,
            )
        except Exception as exc:
            raise RuntimeError(f"Invalid job_id '{job_id}': {exc}") from exc

    # asyncpg returns 'UPDATE <n>'
    try:
        n = int(result.split()[-1])
    except (ValueError, IndexError):
        n = 0

    cancelled = n > 0
    logger.info(
        "grabber.cancel_rotation: job_id=%s cancelled=%s reason=%s",
        job_id,
        cancelled,
        reason,
    )
    return {"ok": True, "cancelled": cancelled}


# ---------------------------------------------------------------------------
# Tool 6 — grabber_health
# ---------------------------------------------------------------------------


@mcp.tool()
async def grabber_health() -> dict[str, Any]:
    """Return a lightweight health summary for the grabber subsystem.

    Suitable for the morning Telegram digest and director health checks.

    Returns:
        ``{status, queued, running, last_run_at, last_run_status, frozen}``

        - ``status``: ``"ok"`` or ``"frozen"`` (when ``GRABBER_FROZEN=1``).
        - ``queued``: count of pending jobs.
        - ``running``: count of running jobs.
        - ``last_run_at``: ISO-8601 timestamp of most recent ended job, or None.
        - ``last_run_status``: status of most recent ended job, or None.
        - ``frozen``: bool — True if ``GRABBER_FROZEN=1`` is set.
    """
    _require_db()
    pool = await _pool()
    frozen = os.environ.get("GRABBER_FROZEN") == "1"

    async with pool.acquire() as conn:
        queued_row = await conn.fetchrow(
            "SELECT COUNT(*)::int AS n FROM fleet.grabber_jobs WHERE status = 'pending'"
        )
        running_row = await conn.fetchrow(
            "SELECT COUNT(*)::int AS n FROM fleet.grabber_jobs WHERE status = 'running'"
        )
        last_row = await conn.fetchrow(
            """
            SELECT status, ended_at
            FROM fleet.grabber_jobs
            WHERE ended_at IS NOT NULL
            ORDER BY ended_at DESC
            LIMIT 1
            """
        )

    return {
        "status": "frozen" if frozen else "ok",
        "queued": queued_row["n"],
        "running": running_row["n"],
        "last_run_at": (
            last_row["ended_at"].isoformat() if last_row and last_row["ended_at"] else None
        ),
        "last_run_status": last_row["status"] if last_row else None,
        "frozen": frozen,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Grabber MCP server. stdio by default; set MCP_TRANSPORT=http
    to expose over HTTP (PORT, default 8084)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8084"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
