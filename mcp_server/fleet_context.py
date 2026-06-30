"""Fleet metadata context builder for the director's system prompt."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from .db import Database

logger = logging.getLogger(__name__)


async def _build_fleet_context(db: Database) -> str:
    """Snapshot of live fleet state, injected into the director's system prompt."""
    try:
        agents = await db.fleet_status()
        events = await db.recent_events(since_minutes=60, agent_name=None, kind=None, limit=30)
        runs = await db.list_runs(agent_name=None, status=None, since_minutes=60 * 24, limit=10)
    except Exception as exc:  # pragma: no cover
        logger.exception("fleet context build failed")
        return f"[fleet state unavailable: {exc}]"

    parts = ["# Live fleet state\n"]
    parts.append(f"Time: {datetime.now(UTC).isoformat(timespec='seconds')}\n")
    parts.append(f"\n## Agents ({len(agents)})\n")
    for a in agents:
        last_run = a.get("last_run") or {}
        parts.append(
            f"- {a['name']} [{a['kind']}] "
            f"deploy={a.get('deploy_target') or '—'} "
            f"last_seen={a.get('last_seen_at') or '—'} "
            f"last_run={last_run.get('kind') or '—'}/{last_run.get('status') or '—'} "
            f"open_intents={a.get('open_intents', 0)}\n"
        )
    parts.append("\n## Recent runs (last 24h, max 10)\n")
    for r in runs:
        parts.append(
            f"- {r.get('agent_name')} {r.get('kind')} {r.get('status')} "
            f"started={r.get('started_at')} "
            f"summary={json.dumps(r.get('summary') or {}, default=str)[:200]}\n"
        )
    parts.append("\n## Recent events (last 60min, max 30)\n")
    for e in events:
        payload = json.dumps(e.get("payload") or {}, default=str)
        if len(payload) > 200:
            payload = payload[:197] + "\u2026"
        parts.append(f"- {e.get('ts')} {e.get('agent_name')} {e.get('kind')} {payload}\n")
    return "".join(parts)
