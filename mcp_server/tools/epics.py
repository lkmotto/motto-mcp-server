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

import logging
import os
import re
from typing import Any
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

# Worker B constants (shared via _AGENT_NAME). When Worker A's branch
# lands the same constants will be re-declared at the top of this file
# and merge cleanly because they have the same values.
_GITHUB_API = "https://api.github.com"
_FACTORY_API_BASE = (
    os.environ.get("FACTORY_API_BASE") or "https://api.factory.ai/api/v0"
).rstrip("/")
_AGENT_NAME = "motto-mcp-server"


# ── GitHub helpers ──────────────────────────────────────────────────────────


def _gh_token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _gh_read_headers() -> dict[str, str]:
    """Headers for GitHub reads; auth attached when a token is configured."""
    h: dict[str, str] = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    tok = _gh_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _gh_write_headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _parse_issue_url(url: str) -> tuple[str | None, int | None]:
    """Pull (owner/repo, issue_number) out of a github issue HTML URL.
    Returns (None, None) on malformed input."""
    if not url:
        return None, None
    try:
        p = urlparse(url)
        if "github.com" not in (p.netloc or ""):
            return None, None
        m = re.match(r"^/([^/]+)/([^/]+)/issues/(\d+)/?$", p.path or "")
        if not m:
            return None, None
        return f"{m.group(1)}/{m.group(2)}", int(m.group(3))
    except (ValueError, TypeError, AttributeError):
        return None, None


async def _fetch_issue_body(repo: str, number: int) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(
                f"{_GITHUB_API}/repos/{repo}/issues/{number}",
                headers=_gh_read_headers(),
            )
            if r.status_code == 200:
                return r.json().get("body") or ""
            logger.warning(
                "github issue fetch %s#%s -> %s", repo, number, r.status_code
            )
            return None
    except httpx.HTTPError as exc:
        logger.warning("github issue fetch error: %s", exc)
        return None


async def _fetch_issue_comments(
    repo: str, number: int, limit: int = 20,
) -> list[dict[str, Any]]:
    """Most recent N comments newest-first."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(
                f"{_GITHUB_API}/repos/{repo}/issues/{number}/comments",
                params={
                    "per_page": min(100, max(1, int(limit))),
                    "sort": "created",
                    "direction": "desc",
                },
                headers=_gh_read_headers(),
            )
            if r.status_code != 200:
                logger.warning(
                    "github comments %s#%s -> %s",
                    repo, number, r.status_code,
                )
                return []
            raw = r.json() or []
    except httpx.HTTPError as exc:
        logger.warning("github comments error: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for c in raw[:limit]:
        out.append({
            "id": c.get("id"),
            "user": (c.get("user") or {}).get("login"),
            "created_at": c.get("created_at"),
            "body": c.get("body") or "",
        })
    return out


async def _post_issue_comment(repo: str, number: int, body: str) -> bool:
    tok = _gh_token()
    if not tok:
        logger.warning(
            "no GITHUB_TOKEN; skipping issue comment on %s#%s", repo, number
        )
        return False
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(
                f"{_GITHUB_API}/repos/{repo}/issues/{number}/comments",
                headers=_gh_write_headers(tok),
                json={"body": body},
            )
            if r.status_code in (200, 201):
                return True
            logger.warning(
                "github comment %s#%s -> %s: %s",
                repo, number, r.status_code, r.text[:200],
            )
            return False
    except httpx.HTTPError as exc:
        logger.warning("github comment error: %s", exc)
        return False


# ── Factory helpers ─────────────────────────────────────────────────────────


def _factory_token() -> str | None:
    return os.environ.get("FACTORY_API_KEY") or os.environ.get("FACTORY_TOKEN")


def _factory_headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


async def _factory_get_session(session_id: str) -> dict[str, Any] | None:
    """Best-effort Factory session fetch. Returns None when no creds; a
    generic {status, http_status} dict on non-2xx HTTP responses."""
    if not session_id:
        return None
    tok = _factory_token()
    if not tok:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(
                f"{_FACTORY_API_BASE}/sessions/{session_id}",
                headers=_factory_headers(tok),
            )
            if r.status_code == 200:
                return r.json()
            return {"status": "unknown", "http_status": r.status_code}
    except httpx.HTTPError as exc:
        logger.info("factory session fetch error: %s", exc)
        return None


async def _factory_interrupt_session(session_id: str) -> dict[str, Any]:
    """Best-effort interrupt droid. Never raises so pause/kill can continue."""
    if not session_id:
        return {"ok": False, "detail": "no factory_session_id"}
    tok = _factory_token()
    if not tok:
        return {"ok": False, "detail": "FACTORY_API_KEY not configured"}
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(
                f"{_FACTORY_API_BASE}/sessions/{session_id}/interrupt",
                headers=_factory_headers(tok),
                json={},
            )
            if r.status_code in (200, 202, 204):
                return {"ok": True, "detail": f"http {r.status_code}"}
            return {
                "ok": False,
                "detail": f"http {r.status_code}: {r.text[:200]}",
            }
    except httpx.HTTPError as exc:
        return {"ok": False, "detail": f"interrupt error: {exc}"}


# ── PR drafting helpers (for kill_epic) ─────────────────────────────────────


async def _list_open_prs_for_issue(
    repo: str, issue_number: int,
) -> list[dict[str, Any]]:
    """Open PRs in `repo` whose body references this issue. Best-effort."""
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            q = f"repo:{repo} is:pr is:open #{issue_number} in:body"
            r = await c.get(
                f"{_GITHUB_API}/search/issues",
                params={"q": q, "per_page": 50},
                headers=_gh_read_headers(),
            )
            if r.status_code != 200:
                logger.warning("pr search %s#%s -> %s", repo, issue_number, r.status_code)
                return []
            items = (r.json() or {}).get("items") or []
    except httpx.HTTPError as exc:
        logger.warning("pr search error: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for it in items:
        if it.get("draft"):
            continue
        out.append({
            "number": it.get("number"),
            "node_id": it.get("node_id"),
            "url": it.get("html_url"),
            "title": it.get("title"),
        })
    return out


async def _convert_pr_to_draft(node_id: str) -> bool:
    """GraphQL convertPullRequestToDraft mutation. Best-effort."""
    tok = _gh_token()
    if not tok or not node_id:
        return False
    mutation = (
        "mutation($id: ID!) { "
        "convertPullRequestToDraft(input: {pullRequestId: $id}) "
        "{ pullRequest { isDraft } } }"
    )
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.post(
                f"{_GITHUB_API}/graphql",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "Accept": "application/json",
                },
                json={"query": mutation, "variables": {"id": node_id}},
            )
            if r.status_code != 200:
                logger.warning(
                    "graphql draft -> %s: %s", r.status_code, r.text[:200]
                )
                return False
            body = r.json()
            if body.get("errors"):
                logger.warning("graphql draft errors: %s", body.get("errors"))
                return False
            return bool(
                ((body.get("data") or {})
                 .get("convertPullRequestToDraft") or {})
                .get("pullRequest", {}).get("isDraft")
            )
    except httpx.HTTPError as exc:
        logger.warning("graphql draft error: %s", exc)
        return False


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
    """Aggregate snapshot of an epic for cockpit + watcher consumption.

    Returns:
      {
        epic: full row from public.epics (incl. status, cost_so_far_usd,
              success_criteria_json, last_progress_at),
        gh: {issue_url, issue_number, repo, body, comments: [last 20]},
        factory_session: {session_id, status, ...} | None,
        fleet_events: [{ts, kind, level, agent_name, run_id, payload}, ...]
                      (most recent first; up to 100),
        cost_so_far_usd, success_criteria_json, last_progress_at,
        last_progress_event: {ts, kind} | None,
      }
    """
    from ..server import db  # late import to avoid circular import at module load

    epic = await db.fetch_epic_for_status(epic_id=int(epic_id))
    if epic is None:
        raise ValueError(f"epic {epic_id} not found")

    repo, issue_number = _parse_issue_url(epic.get("gh_issue_url") or "")
    if issue_number is None and epic.get("gh_issue_number") is not None:
        issue_number = int(epic["gh_issue_number"])

    gh_body: str | None = None
    gh_comments: list[dict[str, Any]] = []
    if repo and issue_number is not None:
        gh_body = await _fetch_issue_body(repo, issue_number)
        gh_comments = await _fetch_issue_comments(repo, issue_number, limit=20)

    factory_session: dict[str, Any] | None = None
    session_id = epic.get("factory_session_id")
    if session_id:
        factory_session = await _factory_get_session(session_id)
        if factory_session is not None:
            factory_session.setdefault("session_id", session_id)

    fleet_events: list[dict[str, Any]] = []
    epic_run_id = epic.get("run_id")
    if epic_run_id:
        fleet_events = await db.events_for_epic_run(
            run_id=epic_run_id, limit=100,
        )

    last_progress_event: dict[str, Any] | None = None
    for ev in fleet_events:
        kind = (ev.get("kind") or "").lower()
        if kind.startswith("epic.") or "progress" in kind:
            last_progress_event = {"ts": ev.get("ts"), "kind": ev.get("kind")}
            break

    return {
        "epic": epic,
        "gh": {
            "issue_url": epic.get("gh_issue_url"),
            "issue_number": epic.get("gh_issue_number"),
            "repo": repo,
            "body": gh_body if gh_body is not None else (epic.get("rationale") or ""),
            "comments": gh_comments,
        },
        "factory_session": factory_session,
        "fleet_events": fleet_events,
        "cost_so_far_usd": epic.get("cost_so_far_usd"),
        "success_criteria_json": epic.get("success_criteria_json"),
        "last_progress_at": epic.get("last_progress_at"),
        "last_progress_event": last_progress_event,
    }


async def pause_epic(
    epic_id: int,
    reason: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Pause an active epic.

      1. set epics.status='paused'
      2. interrupt the locked Factory droid session (best effort)
      3. post a comment on the linked GitHub Issue with the reason
      4. record_event(kind='epic.paused')

    Returns {epic_id, status, factory_interrupt, comment_posted, reason}.
    """
    from ..server import db

    epic = await db.fetch_epic_for_status(epic_id=int(epic_id))
    if epic is None:
        raise ValueError(f"epic {epic_id} not found")

    ok = await db.set_epic_status(
        epic_id=int(epic_id), new_status="paused", reason=reason or "",
    )
    if not ok:
        raise RuntimeError(f"could not pause epic {epic_id} (status update failed)")

    interrupt_result: dict[str, Any] = {"ok": False, "detail": "no factory session"}
    session_id = epic.get("factory_session_id")
    if session_id:
        interrupt_result = await _factory_interrupt_session(session_id)

    repo, issue_number = _parse_issue_url(epic.get("gh_issue_url") or "")
    comment_posted = False
    if repo and issue_number is not None:
        msg = "**Epic paused**"
        if reason:
            msg += f"\n\n> {reason}"
        if session_id:
            msg += (
                f"\n\nFactory session `{session_id}` interrupt: "
                + ("ok" if interrupt_result.get("ok")
                   else interrupt_result.get("detail", "?"))
            )
        comment_posted = await _post_issue_comment(repo, issue_number, msg)

    payload = {
        "epic_id": int(epic_id),
        "status": "paused",
        "reason": reason or "",
        "session_id": session_id,
        "factory_interrupt": interrupt_result,
        "comment_posted": comment_posted,
    }
    await db.record_event(
        agent_name=_AGENT_NAME,
        kind="epic.paused",
        payload=payload,
        run_id=run_id,
        level="info",
    )

    return {
        "epic_id": int(epic_id),
        "status": "paused",
        "factory_interrupt": interrupt_result,
        "comment_posted": comment_posted,
        "reason": reason or "",
    }


async def kill_epic(
    epic_id: int,
    reason: str = "",
    run_id: str | None = None,
) -> dict[str, Any]:
    """Kill an epic.

      1. set epics.status='abandoned' + closed_reason=reason
         (the underlying CHECK constraint disallows literal 'killed', so we
         land on the canonical 'abandoned' terminal state)
      2. interrupt the locked Factory droid session (best effort)
      3. convert any open PRs that reference the issue body to draft
      4. post a kill notice on the linked GitHub Issue
      5. record_event(kind='epic.killed')

    Returns {epic_id, status, factory_interrupt, drafted_prs,
    comment_posted, reason}.
    """
    from ..server import db

    epic = await db.fetch_epic_for_status(epic_id=int(epic_id))
    if epic is None:
        raise ValueError(f"epic {epic_id} not found")

    ok = await db.set_epic_status(
        epic_id=int(epic_id), new_status="abandoned", reason=reason or "",
    )
    if not ok:
        raise RuntimeError(f"could not kill epic {epic_id} (status update failed)")

    interrupt_result: dict[str, Any] = {"ok": False, "detail": "no factory session"}
    session_id = epic.get("factory_session_id")
    if session_id:
        interrupt_result = await _factory_interrupt_session(session_id)

    repo, issue_number = _parse_issue_url(epic.get("gh_issue_url") or "")
    drafted_prs: list[dict[str, Any]] = []
    if repo and issue_number is not None:
        prs = await _list_open_prs_for_issue(repo, issue_number)
        for pr in prs:
            node_id = pr.get("node_id")
            ok_draft = False
            if node_id:
                ok_draft = await _convert_pr_to_draft(node_id)
            drafted_prs.append({
                "number": pr.get("number"),
                "url": pr.get("url"),
                "title": pr.get("title"),
                "drafted": bool(ok_draft),
            })

    comment_posted = False
    if repo and issue_number is not None:
        msg = "**Epic killed (abandoned)**"
        if reason:
            msg += f"\n\n> {reason}"
        if session_id:
            msg += (
                f"\n\nFactory session `{session_id}` interrupt: "
                + ("ok" if interrupt_result.get("ok")
                   else interrupt_result.get("detail", "?"))
            )
        if drafted_prs:
            lines = [
                f"- #{pr['number']} ({'drafted' if pr['drafted'] else 'left as-is'})"
                for pr in drafted_prs
            ]
            msg += "\n\nOpen PRs touched:\n" + "\n".join(lines)
        comment_posted = await _post_issue_comment(repo, issue_number, msg)

    payload = {
        "epic_id": int(epic_id),
        "status": "abandoned",
        "reason": reason or "",
        "session_id": session_id,
        "factory_interrupt": interrupt_result,
        "drafted_prs": drafted_prs,
        "comment_posted": comment_posted,
    }
    await db.record_event(
        agent_name=_AGENT_NAME,
        kind="epic.killed",
        payload=payload,
        run_id=run_id,
        level="info",
    )

    return {
        "epic_id": int(epic_id),
        "status": "abandoned",
        "factory_interrupt": interrupt_result,
        "drafted_prs": drafted_prs,
        "comment_posted": comment_posted,
        "reason": reason or "",
    }
