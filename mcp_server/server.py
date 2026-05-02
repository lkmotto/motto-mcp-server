"""Motto fleet-coordination MCP server.

Backed by Neon Postgres. Variable agents (motto-director, motto-sdr-agent,
motto-social-agent) call these tools to register, heartbeat, open/close
runs, emit events, and post cross-agent intent signals. motto-director
also reads fleet status here to drive its perceive→ideate→act loop.

Run: `motto-mcp-server` (HTTP on $PORT). Schema is auto-applied on start.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP

from .db import Database

logger = logging.getLogger(__name__)

db = Database()


@asynccontextmanager
async def _lifespan(_app):
    await db.connect()
    await db.apply_migrations()
    logger.info("motto-fleet ready: schema applied")
    try:
        yield
    finally:
        await db.close()


mcp = FastMCP(
    "motto-fleet",
    instructions=(
        "Fleet-coordination tools backed by Neon Postgres. "
        "Variable agents register, heartbeat, open runs, emit events, "
        "and post cross-agent intents. motto-director consumes via "
        "get_fleet_status / get_recent_events."
    ),
    lifespan=_lifespan,
)


@mcp.tool
async def register_agent(
    name: str,
    kind: str,
    deploy_target: str | None = None,
    version: str | None = None,
) -> dict[str, Any]:
    """Idempotent agent registration. `kind` is 'variable' or 'deterministic'."""
    if kind not in ("variable", "deterministic"):
        raise ValueError("kind must be 'variable' or 'deterministic'")
    row = await db.upsert_agent(
        name=name, kind=kind, deploy_target=deploy_target, version=version
    )
    return {"agent_id": row["id"], "name": row["name"]}


@mcp.tool
async def heartbeat(agent_name: str, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mark an agent alive. status is a free-form jsonb blob (merged into metadata)."""
    await db.heartbeat(agent_name=agent_name, status=status or {})
    return {"ok": True}


@mcp.tool
async def record_run_start(
    agent_name: str,
    kind: str,
    intent: str | None = None,
    langfuse_trace_id: str | None = None,
    parent_run_id: str | None = None,
) -> dict[str, str]:
    """Open a fleet run row. Caller stores run_id, calls record_run_end on completion."""
    run_id = await db.start_run(
        agent_name=agent_name,
        kind=kind,
        intent=intent,
        langfuse_trace_id=langfuse_trace_id,
        parent_run_id=parent_run_id,
    )
    return {"run_id": str(run_id)}


@mcp.tool
async def record_run_end(
    run_id: str,
    status: str,
    summary: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Close a fleet run. status is 'success' / 'error' / 'cancelled'."""
    if status not in ("success", "error", "cancelled"):
        raise ValueError("status must be success / error / cancelled")
    await db.end_run(run_id=run_id, status=status, summary=summary or {})
    return {"ok": True}


@mcp.tool
async def record_event(
    agent_name: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    run_id: str | None = None,
    level: str = "info",
) -> dict[str, int]:
    """Record a fine-grained fleet event. Optional run_id to attach to a run."""
    event_id = await db.record_event(
        agent_name=agent_name,
        kind=kind,
        payload=payload or {},
        run_id=run_id,
        level=level,
    )
    return {"event_id": event_id}


@mcp.tool
async def signal_intent(
    target_agent: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    source_agent: str | None = None,
) -> dict[str, str]:
    """Post a cross-agent nudge. Director uses this to direct other agents."""
    intent_id = await db.signal_intent(
        target_agent=target_agent,
        kind=kind,
        payload=payload or {},
        source_agent=source_agent,
    )
    return {"intent_id": str(intent_id)}


@mcp.tool
async def consume_open_intents(agent_name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Atomically claim and mark consumed every open intent targeting this agent."""
    return await db.consume_intents(agent_name=agent_name, limit=limit)


@mcp.tool
async def get_fleet_status() -> list[dict[str, Any]]:
    """Snapshot of every registered agent: kind, last_seen_at, last run, open intents."""
    return await db.fleet_status()


@mcp.tool
async def get_recent_events(
    since_minutes: int = 60,
    agent_name: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Recent fleet events, newest first. Used by director for perceive()."""
    return await db.recent_events(
        since_minutes=since_minutes,
        agent_name=agent_name,
        kind=kind,
        limit=limit,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    port = int(os.environ.get("PORT", "8080"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
