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

import json
import os
from typing import Any

import httpx

_GITHUB_API = "https://api.github.com"
_FACTORY_API_BASE = (
    os.environ.get("FACTORY_API_BASE") or "https://api.factory.ai/api/v0"
).rstrip("/")
_DEFAULT_COMPUTER_ID = (
    os.environ.get("FACTORY_COMPUTER_ID")
    or os.environ.get("LEGION_COMPUTER_ID")
    or "fc715237-e805-47f3-a590-0b2561fea3e0"
)
_AGENT_NAME = "motto-mcp-server"
_EPIC_LABEL = "epic"


def _gh_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _gh_headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _factory_token() -> str | None:
    return os.environ.get("FACTORY_API_KEY")


def _factory_headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tok}",
        "Content-Type": "application/json",
    }


def _merge_labels(labels: list[str] | None) -> list[str]:
    out: list[str] = [_EPIC_LABEL]
    for lab in labels or []:
        lab = (lab or "").strip()
        if lab and lab not in out:
            out.append(lab)
    return out


def _build_dispatch_prompt(epic: dict[str, Any], override: str | None) -> str:
    if override:
        return override
    title = epic.get("title") or "(untitled epic)"
    issue_url = epic.get("gh_issue_url") or ""
    issue_num = epic.get("gh_issue_number")
    body = epic.get("rationale") or ""
    crit = epic.get("success_criteria_json") or []
    if isinstance(crit, str):
        try:
            crit = json.loads(crit)
        except (TypeError, ValueError):
            crit = []
    max_cost = epic.get("max_cost_usd")
    max_hours = epic.get("max_hours")

    crit_block = (
        "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(crit))
        or "  (none specified)"
    )
    budget_block = (
        f"  max_cost_usd: {max_cost if max_cost is not None else 'unset'}\n"
        f"  max_hours:    {max_hours if max_hours is not None else 'unset'}"
    )
    return (
        f"You are a Factory droid executing epic #{epic.get('id')} "
        f"({issue_url or 'no issue url'}).\n\n"
        f"# Title\n{title}\n\n"
        f"# GitHub Issue\n"
        f"  url:    {issue_url or 'unset'}\n"
        f"  number: {issue_num if issue_num is not None else 'unset'}\n\n"
        f"# Budget\n{budget_block}\n\n"
        f"# Success criteria\n{crit_block}\n\n"
        f"# Brief / body\n{body or '(empty)'}\n\n"
        "When done, comment on the GitHub Issue with a self-grade of each "
        "success criterion (PASS/FAIL/PARTIAL + 1-line evidence), then stop."
    )


def _extract_session_id(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("sessionId", "id", "session_id"):
        sid = payload.get(key)
        if isinstance(sid, str) and sid:
            return sid
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("sessionId", "id", "session_id"):
            sid = data.get(key)
            if isinstance(sid, str) and sid:
                return sid
    return ""


def _extract_status(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("status", "state"):
        v = payload.get(key)
        if isinstance(v, str):
            return v
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("status", "state"):
            v = data.get(key)
            if isinstance(v, str):
                return v
    return ""


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
    filed_by: str = "agent",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Create a GitHub Issue with label `epic` and insert a row into the
    epics table linked by gh_issue_url.

    Returns {epic_id, issue_url, issue_number, status}. Labels are auto-merged
    with `epic`. `filed_by` controls the initial status:
      - "agent" (default) -> status='proposed' (awaiting human approval)
      - "human"/"luke"   -> status='active'   (immediately dispatchable)
    """
    if not title or not title.strip():
        raise ValueError("title is required")
    if not repo_full_name or "/" not in repo_full_name:
        raise ValueError("repo_full_name must be 'owner/repo'")

    tok = _gh_token()
    if not tok:
        raise RuntimeError(
            "create_epic needs GITHUB_TOKEN (or GH_TOKEN) to file the issue"
        )

    merged_labels = _merge_labels(labels)
    issue_body = body or ""

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            f"{_GITHUB_API}/repos/{repo_full_name}/issues",
            headers=_gh_headers(tok),
            json={
                "title": title.strip(),
                "body": issue_body,
                "labels": merged_labels,
            },
        )
        if r.status_code >= 300:
            raise RuntimeError(
                f"github issue create failed: {r.status_code} {r.text[:300]}"
            )
        issue = r.json()

    issue_number = int(issue.get("number"))
    issue_url = issue.get("html_url") or issue.get("url") or ""

    initial_status = (
        "active" if (filed_by or "").lower() in ("human", "luke", "cockpit")
        else "proposed"
    )

    from ..server import db  # late import to avoid circular import at module load

    await db.upsert_agent(
        name=_AGENT_NAME, kind="deterministic", deploy_target=None, version=None
    )
    row = await db.insert_epic_with_gh(
        title=title.strip(),
        repo_full_name=repo_full_name,
        body=issue_body,
        gh_issue_url=issue_url,
        gh_issue_number=issue_number,
        labels=merged_labels,
        success_criteria=success_criteria,
        kpi_ref=kpi_ref,
        max_cost_usd=max_cost_usd,
        max_hours=max_hours,
        status=initial_status,
        run_id=run_id,
    )

    epic_id = int(row["id"])
    payload = {
        "epic_id": epic_id,
        "issue_url": issue_url,
        "issue_number": issue_number,
        "repo": repo_full_name,
        "status": row["status"],
        "filed_by": filed_by,
        "priority": priority,
        "max_cost_usd": float(max_cost_usd),
        "max_hours": int(max_hours),
    }
    await db.record_event(
        agent_name=_AGENT_NAME,
        kind="epic.created",
        payload=payload,
        run_id=run_id,
        level="info",
    )
    return {
        "epic_id": epic_id,
        "issue_url": issue_url,
        "issue_number": issue_number,
        "status": row["status"],
    }


async def dispatch_droid_for_epic(
    epic_id: int,
    computer_id: str | None = None,
    model: str = "claude-opus-4-7",
    autonomy: str = "high",
    reasoning_effort: str = "high",
    prompt_override: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Spawn a Factory droid session for an epic and lock the epic to it.

    Reads the epic row, builds a prompt from the issue body + epic spec,
    POSTs to the Factory API to create a session and seed the initial
    message, then sets epics.factory_session_id (only if currently NULL,
    so the epic is locked to a single droid).

    Returns {session_id, status, computer_id, locked}. `locked=false` means
    the epic was already locked to a different session; the session was NOT
    spawned in that case.
    """
    from ..server import db  # late import to avoid circular import at module load

    epic = await db.fetch_epic_dispatch_row(epic_id=int(epic_id))
    if epic is None:
        raise ValueError(f"epic {epic_id} not found")

    existing = epic.get("factory_session_id")
    if existing:
        return {
            "session_id": existing,
            "status": "already_locked",
            "computer_id": computer_id or _DEFAULT_COMPUTER_ID,
            "locked": False,
        }

    tok = _factory_token()
    if not tok:
        raise RuntimeError(
            "dispatch_droid_for_epic needs FACTORY_API_KEY to spawn a session"
        )

    prompt = _build_dispatch_prompt(epic, prompt_override)
    target_computer = computer_id or _DEFAULT_COMPUTER_ID
    tags = [f"epic-{int(epic_id)}", "motto-fleet"]

    spawn_body: dict[str, Any] = {
        "computerId": target_computer,
        "sessionSettings": {
            "tags": [{"name": t} for t in tags],
            "model": model,
            "autonomy": autonomy,
            "reasoningEffort": reasoning_effort,
        },
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        sess_resp = await client.post(
            f"{_FACTORY_API_BASE}/sessions",
            headers=_factory_headers(tok),
            json=spawn_body,
        )
        if sess_resp.status_code >= 300:
            raise RuntimeError(
                f"factory session create failed: {sess_resp.status_code} "
                f"{sess_resp.text[:300]}"
            )
        session = sess_resp.json()

        session_id = _extract_session_id(session)
        if not session_id:
            raise RuntimeError(
                f"factory session response missing sessionId: {session!r}"
            )

        msg_resp = await client.post(
            f"{_FACTORY_API_BASE}/sessions/{session_id}/messages",
            headers=_factory_headers(tok),
            json={"text": prompt},
        )
        if msg_resp.status_code >= 300:
            raise RuntimeError(
                f"factory session message failed: {msg_resp.status_code} "
                f"{msg_resp.text[:300]}"
            )

    await db.upsert_agent(
        name=_AGENT_NAME, kind="deterministic", deploy_target=None, version=None
    )
    locked = await db.update_epic_session_id(
        epic_id=int(epic_id), factory_session_id=session_id
    )
    status = _extract_status(session) or "spawned"

    await db.record_event(
        agent_name=_AGENT_NAME,
        kind="epic.dispatched",
        payload={
            "epic_id": int(epic_id),
            "session_id": session_id,
            "computer_id": target_computer,
            "model": model,
            "autonomy": autonomy,
            "reasoning_effort": reasoning_effort,
            "locked": locked,
            "status": status,
        },
        run_id=run_id,
        level="info",
    )

    return {
        "session_id": session_id,
        "status": status,
        "computer_id": target_computer,
        "locked": locked,
    }


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
