"""Epic control plane MCP tools (Day-0 bootstrap, Workers A+B).

Tools:
  create_epic              (Worker A) - GH Issue creation + epics row insert
  dispatch_droid_for_epic  (Worker A) - Factory API spawn + session lock
  epic_status              (Worker B) - aggregate GH + Factory + fleet data
  pause_epic               (Worker B) - status update + GH comment
  kill_epic                (Worker B) - status update + Factory cancel + GH comment

Every state-changing tool must call record_event so the cockpit can replay.
"""

from __future__ import annotations

from typing import Any


async def create_epic(
    title: str,
    repo_full_name: str,
    body: str,
    labels: list[str] | None = None,
    success_criteria: list[str] | None = None,
    kpi_ref: str = "",
    max_cost_usd: float = 25.0,
    max_hours: int = 8,
    priority: str = "medium",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create a GitHub Issue with label epic and insert a row into the epics
    table linked by gh_issue_url.

    Returns {epic_id, issue_url, issue_number}. Labels are auto-merged with
    [epic]. The body should be structured with success criteria and steps.
    """
    raise NotImplementedError("Worker A: implement create_epic")


async def dispatch_droid_for_epic(
    epic_id: int,
    prompt_override: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Spawn a Factory droid session with motto-mcp-server attached via HTTP
    MCP. Locks the epic to that session via epics.factory_session_id.

    Returns {session_id, status, computer_id} from Factory API.
    """
    raise NotImplementedError("Worker A: implement dispatch_droid_for_epic")


async def epic_status(epic_id: int) -> dict[str, Any]:
    """Return a JSON blob with: issue body, all issue comments, latest droid
    session status from Factory, all fleet events tagged with this epic_id,
    and cost-so-far estimate.
    """
    raise NotImplementedError("Worker B: implement epic_status")


async def pause_epic(
    epic_id: int,
    reason: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Pause an active epic. Sets status to paused and posts an issue comment
    with the pause reason.
    """
    raise NotImplementedError("Worker B: implement pause_epic")


async def kill_epic(
    epic_id: int,
    reason: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Kill/abandon an epic. Sets status to abandoned, posts a comment on the
    linked GitHub Issue, and cancels the associated Factory session if still
    running.
    """
    raise NotImplementedError("Worker B: implement kill_epic")
