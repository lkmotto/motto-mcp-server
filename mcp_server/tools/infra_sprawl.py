"""Infra-sprawl control MCP tools (Day-0 bootstrap, Worker C).

Tools:
  list_all_services     - unified NF + CF service listing
  find_orphans          - stale/unused service detection
  archive_service       - dry-run+safe service archival
  consolidation_audit   - LLM-driven duplication analysis
"""

from __future__ import annotations

from typing import Any


async def list_all_services() -> list[dict[str, Any]]:
    """List all services across Northflank projects, jobs, and Cloudflare
    Workers. Returns unified list with: name, kind, last_deployed_at,
    has_recent_runs, repo_link if discoverable.
    """
    raise NotImplementedError("Worker C: implement list_all_services")


async def find_orphans(
    days_since_run: int = 30,
    days_since_commit: int = 60,
) -> list[dict[str, Any]]:
    """Find services with no run in N days, no commit in M days, and no
    env var consumers (via secret-group inheritance). Returns candidate list.
    """
    raise NotImplementedError("Worker C: implement find_orphans")


async def archive_service(
    name: str,
    reason: str = "",
    confirmed: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Archive a service. Default is dry-run (confirmation_required event).
    Set confirmed=true to actually delete via Northflank API.
    Returns the record_event payload so the cockpit can replay.
    """
    raise NotImplementedError("Worker C: implement archive_service")


async def consolidation_audit() -> list[dict[str, Any]]:
    """LLM-driven audit (DeepSeek V4-Flash). Reads list_all_services() +
    repo READMEs + skill descriptions, returns clusters of duplication
    candidates.
    """
    raise NotImplementedError("Worker C: implement consolidation_audit")
