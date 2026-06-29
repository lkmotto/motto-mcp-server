"""Motto fleet-coordination MCP server.

Backed by Neon Postgres. Variable agents (motto-director, motto-sdr-agent,
motto-social-agent) call these tools to register, heartbeat, open/close
runs, emit events, and post cross-agent intent signals. motto-director
also reads fleet status here to drive its perceive→ideate→act loop.

Run: `motto-mcp-server` (HTTP on $PORT). Schema is auto-applied on start.

HTTP surface:
    /mcp/                       FastMCP streamable-http transport (auth required)
    /dashboard                  Read-only HTML fleet dashboard (auth required)
    /cockpit                    Interactive cockpit UI (chat + intent submit)
    /cockpit/state.json         Cockpit live state (auth required)
    /cockpit/chat               POST: chat with director (Claude Max OAuth)
    /cockpit/intent             POST: submit a manual intent / nudge
    /fleet/status.json          Same data as /dashboard but JSON (auth required)
    /healthz                    Liveness probe (open)

Auth: every non-/healthz endpoint requires either
  Authorization: Bearer <MOTTO_MCP_AUTH_TOKEN>   (preferred for MCP clients)
  ?token=<MOTTO_MCP_AUTH_TOKEN>                  (works for browser dashboard)
If MOTTO_MCP_AUTH_TOKEN is unset, all paths are open (dev/local mode).
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from html import escape as h
from typing import Any

from fastmcp.server import FastMCP
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .db import Database
from .routes import register_routes as _register_cockpit_routes

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


def _expected_auth_token() -> str | None:
    """Fetch the configured bearer token (legacy alias supported)."""
    return os.environ.get("MOTTO_MCP_AUTH_TOKEN") or os.environ.get("MCP_AUTH_TOKEN")


def _mcp_auth() -> StaticTokenVerifier | None:
    """Protect /mcp when a token is configured; stay open for local/dev."""
    token = _expected_auth_token()
    if not token:
        return None
    return StaticTokenVerifier(
        tokens={
            token: {
                "client_id": "motto-mcp-client",
                "scopes": [],
            }
        }
    )


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


# ── MCP tools ─────────────────────────────────────────────────────────────────────────────────


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
    try:
        from uuid import UUID as _UUID
        _UUID(run_id)
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"record_run_end: run_id {run_id!r} is not a valid UUID. "
            "Pass the run_id returned by record_run_start."
        ) from exc
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
async def record_artifact_content(
    agent_name: str,
    kind: str,
    body: str,
    name: str | None = None,
    run_id: str | None = None,
    intent: str | None = None,
    repo: str | None = None,
    meta: dict[str, Any] | None = None,
    send_blocking: bool = False,
) -> dict[str, int]:
    """Record an actual output artifact (PR diff, draft email, script, comp
    narrative, etc.) inline in fleet.artifacts.content so the director's
    output_critic lens can read and judge it.

    - kind: short label, e.g. 'cold_email', 'pr_diff', 'video_script',
      'comp_narrative', 'amc_reply_draft'.
    - body: the actual text. Capped at ~1MB; longer bodies are truncated
      with truncated=true in the stored content.
    - intent: what the producing agent was trying to do, used by the
      critic to judge fit-to-intent.
    - repo: optional source repo (e.g. lkmotto/motto-sdr-agent) so the
      critic knows where to file a critique issue.
    - send_blocking: if true, downstream code MUST check review_status
      before dispatching (e.g. SDR cold-send, AMC email-send).
    """
    artifact_id = await db.record_artifact_content(
        agent_name=agent_name,
        kind=kind,
        name=name,
        body=body,
        run_id=run_id,
        intent=intent,
        repo=repo,
        meta=meta,
        send_blocking=send_blocking,
    )
    return {"artifact_id": int(artifact_id)}


@mcp.tool
async def artifacts_pending_review(
    since_hours: int = 24,
    limit: int = 25,
    agent_name: str | None = None,
) -> list[dict[str, Any]]:
    """Recent artifacts not yet critiqued. Director's output_critic lens."""
    return await db.artifacts_pending_review(
        since_hours=since_hours,
        limit=limit,
        agent_name=agent_name,
    )


@mcp.tool
async def mark_artifact_reviewed(
    artifact_id: int,
    review_status: str,
    critique: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Set content.review_status to 'passed' | 'flagged' | 'blocked'
    after critique. Optional critique payload (issues[], severity, etc).
    """
    ok = await db.mark_artifact_reviewed(
        artifact_id=artifact_id,
        review_status=review_status,
        critique=critique,
    )
    return {"ok": bool(ok)}


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


# ── Read / debug / replay tools ────────────────────────────────────────────────────────────────


@mcp.tool
async def list_runs(
    agent_name: str | None = None,
    status: str | None = None,
    since_minutes: int = 60 * 24,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recent runs, newest first. Filter by agent_name and/or status."""
    return await db.list_runs(
        agent_name=agent_name,
        status=status,
        since_minutes=since_minutes,
        limit=limit,
    )


@mcp.tool
async def get_run(run_id: str) -> dict[str, Any]:
    """Full run record + linked events + decisions + artifacts. For deep debugging."""
    return await db.get_run(run_id=run_id)


@mcp.tool
async def get_decisions(
    run_id: str | None = None,
    agent_name: str | None = None,
    choice: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Audit trail of material choices. Filter by run_id, agent, or choice kind."""
    return await db.get_decisions(
        run_id=run_id,
        agent_name=agent_name,
        choice=choice,
        limit=limit,
    )


@mcp.tool
async def get_locks() -> list[dict[str, Any]]:
    """Currently held locks (resource, holder run, expires_at). Excludes expired."""
    return await db.get_locks()


@mcp.tool
async def force_release_lock(resource: str) -> dict[str, bool]:
    """Admin: release a stuck lock. Caller is responsible — no run_id check."""
    released = await db.force_release_lock(resource=resource)
    return {"released": released}


@mcp.tool
async def replay_run(run_id: str) -> dict[str, Any]:
    """Replay bundle for a run: full record + events + decisions + artifacts,
    plus parent_run and child_runs. Doesn't actually re-execute — caller's job."""
    return await db.replay_run(run_id=run_id)


# ── local-task queue (motto-local laptop bridge) ────────────────────────────────────


@mcp.tool
async def queue_local_task(
    kind: str,
    payload: dict[str, Any],
    description: str | None = None,
    source: str = "motto-director",
    dedup_key: str | None = None,
    ttl_seconds: int = 600,
) -> dict[str, Any]:
    """Queue a task for the user's local runner (motto-local).

    Standard kinds the runner supports: 'shell', 'read_file', 'write_file',
    'screenshot', 'ocr', 'claude_code', 'browser', 'echo'. The runner
    polls every ~1s and executes claimed tasks on the user's laptop.
    """
    return await db.queue_local_task(
        kind=kind,
        payload=payload,
        source=source,
        description=description,
        dedup_key=dedup_key,
        ttl_seconds=ttl_seconds,
    )


@mcp.tool
async def claim_local_tasks(
    runner_id: str,
    kinds: list[str] | None = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """Atomically claim queued tasks for a local runner. Used by motto-local
    every poll cycle.
    """
    return await db.claim_local_tasks(runner_id=runner_id, kinds=kinds, limit=limit)


@mcp.tool
async def complete_local_task(
    task_id: str,
    status: str,
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, bool]:
    """Mark a local task succeeded/failed/cancelled with its result."""
    ok = await db.complete_local_task(
        task_id=task_id, status=status, result=result, error=error
    )
    return {"ok": ok}


@mcp.tool
async def get_local_task(task_id: str) -> dict[str, Any] | None:
    """Fetch a local task's full record including its result."""
    return await db.get_local_task(task_id=task_id)


@mcp.tool
async def list_local_tasks(
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Recent local tasks, newest first. Filter by status and/or kind."""
    return await db.list_local_tasks(status=status, kind=kind, limit=limit)


# ── Verify_move framework (May 2026) ──────────────────────────────────────────────────────────
# Closes the propose → approve → apply → ??? loop. After a move is applied,
# director (or the cockpit) calls verify_move(move_id) and the dispatcher
# in mcp_server.verifiers picks the right verifier for the move's kind.
# Verifiers may need resources (API keys, OAuth tokens) they don't have —
# they file capability_requests that a human grants in the cockpit.
#
# Day 1 ships only `noop` (auto-pass) and `merge_pr` (gh CI check). Real
# per-repo verifiers come reactively when director files a capability
# request and a human approves it.


import time
import httpx
from .verifiers import VerifyContext, dispatch as _dispatch_verifier


@mcp.tool
async def verify_move(
    move_id: int,
    requested_by: str = "director",
) -> dict[str, Any]:
    """Run the appropriate verifier for an applied pending_moves row and
    record the outcome. Returns the verification record (status,
    evidence, kpi_delta, error). Idempotent only by timestamp — calling
    twice produces two rows; the cockpit / trust score logic should
    look at the most recent row."""
    move = await db.fetch_pending_move(int(move_id))
    if not move:
        return {"ok": False, "error": f"move {move_id} not found"}
    repo = move.get("repo") or ""
    kind = move.get("kind") or ""

    started = time.time()
    async with httpx.AsyncClient(timeout=20.0) as client:
        async def _http_get(url: str, **kw: Any) -> Any:
            return await client.get(url, **kw)

        async def _http_post(url: str, **kw: Any) -> Any:
            return await client.post(url, **kw)

        async def _request_capability(
            capability: str, justification: str, repo_arg: str | None = None
        ) -> int:
            row = await db.file_capability_request(
                capability=capability,
                justification=justification,
                requested_by=requested_by,
                repo=repo_arg or repo or None,
                move_id=int(move_id),
            )
            return int(row["id"])

        ctx = VerifyContext(
            db=db,
            http_get=_http_get,
            http_post=_http_post,
            request_capability=_request_capability,
            requested_by=requested_by,
        )
        result = await _dispatch_verifier(move, ctx)

    duration_ms = int((time.time() - started) * 1000)
    rec = await db.record_verification(
        move_id=int(move_id),
        repo=repo,
        kind=kind,
        verifier=result.verifier,
        status=result.status,
        evidence=result.evidence,
        kpi_delta=result.kpi_delta,
        error=result.error,
        duration_ms=duration_ms,
        requested_by=requested_by,
    )

    # Trust score nudges only on definitive outcomes.
    if result.status in ("passed", "failed"):
        passed = result.status == "passed"
        await db.update_trust_score(scope="global", passed=passed)
        if repo:
            await db.update_trust_score(scope=repo, passed=passed)

    rec["status"] = result.status
    rec["verifier"] = result.verifier
    rec["evidence"] = result.evidence
    rec["kpi_delta"] = result.kpi_delta
    rec["error"] = result.error
    rec["ok"] = True
    return rec


@mcp.tool
async def claim_next_step(
    runner_id: str,
    kinds: list[str] | None = None,
    limit: int = 1,
) -> dict[str, Any]:
    """Atomically claim the next approved pending_moves row(s) for a droid runner.

    Returns {"ok": True, "claimed": [<rows>], "count": N}. Returns
    {"ok": True, "claimed": [], "count": 0} when the queue is empty —
    this is NOT an error, droids should poll on a backoff.
    """
    if not runner_id or not runner_id.strip():
        return {"ok": False, "error": "runner_id must be non-empty"}
    if not (1 <= int(limit) <= 10):
        return {"ok": False, "error": "limit must be between 1 and 10"}
    claimed = await db.director_claim_next_step(
        runner_id=runner_id,
        kinds=kinds,
        limit=int(limit),
    )
    return {"ok": True, "claimed": claimed, "count": len(claimed)}


@mcp.tool
async def release_claimed_step(
    move_id: int,
    runner_id: str,
    reason: str = "",
) -> dict[str, Any]:
    """Release a previously-claimed move back to 'approved' so another
    runner can pick it up. Idempotent — releasing an already-released
    row returns ok=True with released=False.
    """
    if not runner_id or not runner_id.strip():
        return {"ok": False, "error": "runner_id must be non-empty"}
    released = await db.director_release_claim(
        move_id=int(move_id),
        runner_id=runner_id,
        reason=reason,
    )
    return {"ok": True, "released": released}


@mcp.tool
async def list_verifications(
    move_id: int | None = None,
    repo: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List recent move verifications, newest first. Filter by move_id,
    repo, or status (passed/failed/inconclusive/error)."""
    return await db.list_verifications(
        move_id=move_id, repo=repo, status=status, limit=limit
    )


@mcp.tool
async def request_capability(
    capability: str,
    justification: str,
    requested_by: str = "director",
    repo: str | None = None,
    move_id: int | None = None,
) -> dict[str, Any]:
    """Director (or any agent) files a request for a connector / API key /
    OAuth scope it needs to perform a verification or move. Idempotent on
    capability+pending: a duplicate request returns the existing pending
    row instead of opening a new one. The cockpit grants or denies."""
    return await db.file_capability_request(
        capability=capability,
        justification=justification,
        requested_by=requested_by,
        repo=repo,
        move_id=move_id,
    )


@mcp.tool
async def list_capability_requests(
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List capability requests, newest first. status='pending' shows the
    queue waiting for human grant."""
    return await db.list_capability_requests(status=status, limit=limit)


@mcp.tool
async def decide_capability_request(
    request_id: int,
    decision: str,
    decided_by: str,
    grant_detail: str | None = None,
    deny_reason: str | None = None,
) -> dict[str, bool]:
    """Grant or deny a capability request. decision='granted' should also
    include grant_detail describing how it was satisfied (e.g. 'env:
    POSTMARK_API_KEY set in motto-mcp-server'). Cockpit calls this."""
    ok = await db.decide_capability_request(
        request_id=request_id,
        decision=decision,
        decided_by=decided_by,
        grant_detail=grant_detail,
        deny_reason=deny_reason,
    )
    return {"ok": ok}


@mcp.tool
async def get_trust_scores(scope: str | None = None) -> list[dict[str, Any]]:
    """Return rolling trust scores per scope (global + each repo). EWMA
    over verify.passed / verify.failed events."""
    return await db.get_trust_scores(scope=scope)


# ── HTTP custom routes (dashboard + status JSON + healthz) ─────────────────────────────────────


def _auth_ok(request: Request) -> bool:
    """Accept Bearer header or ?token= query for the configured auth token.

    When the env var is unset, all requests pass (dev/local mode).
    """
    expected = _expected_auth_token()
    if not expected:
        return True
    auth_header = request.headers.get("authorization", "")
    if auth_header == f"Bearer {expected}":
        return True
    if request.query_params.get("token") == expected:
        return True
    return False


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request):
    return PlainTextResponse("ok")


@mcp.custom_route("/fleet/status.json", methods=["GET"])
async def fleet_status_json(request: Request):
    if not _auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    agents = await db.fleet_status()
    events = await db.recent_events(
        since_minutes=60, agent_name=None, kind=None, limit=200
    )
    return JSONResponse({
        "now": datetime.now(timezone.utc).isoformat(),
        "agents": agents,
        "recent_events": events,
    })


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request: Request):
    if not _auth_ok(request):
        return HTMLResponse(
            "<h1>unauthorized</h1>"
            "<p>pass <code>?token=&lt;MOTTO_MCP_AUTH_TOKEN&gt;</code> "
            "or send <code>Authorization: Bearer &lt;token&gt;</code>.</p>",
            status_code=401,
        )
    agents = await db.fleet_status()
    events = await db.recent_events(
        since_minutes=60, agent_name=None, kind=None, limit=100
    )
    return HTMLResponse(_render_dashboard(agents, events))


def _fmt_age(ts: str | None) -> str:
    """Render an ISO-8601 timestamp as a relative age (e.g. '12s ago', '3h ago')."""
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        delta = (datetime.now(timezone.utc) - dt).total_seconds()
        if delta < 0:
            return "now"
        if delta < 60:
            return f"{int(delta)}s ago"
        if delta < 3600:
            return f"{int(delta / 60)}m ago"
        if delta < 86400:
            return f"{int(delta / 3600)}h ago"
        return f"{int(delta / 86400)}d ago"
    except Exception:
        return ts


def _render_dashboard(agents: list[dict[str, Any]], events: list[dict[str, Any]]) -> str:
    """Render a single-file dashboard. No JS. Refreshes via meta tag every 30s.

    All user-supplied strings are HTML-escaped to prevent injection — agent
    names and event payloads come from caller-controlled MCP tool input, so
    don't trust them.
    """
    def _kind(k: str) -> str:
        color = "#0a7" if k == "variable" else "#666"
        weight = "600" if k == "variable" else "400"
        return f'<span style="color:{color};font-weight:{weight}">{h(k)}</span>'

    def _status(s: str) -> str:
        colors = {
            "success": "#0a7",
            "error": "#c33",
            "running": "#06b",
            "cancelled": "#666",
        }
        return f'<span style="color:{colors.get(s, "#666")};font-weight:600">{h(s)}</span>'

    def _level(lv: str) -> str:
        colors = {"debug": "#999", "info": "#222", "warn": "#a60", "error": "#c33"}
        return f'<span style="color:{colors.get(lv, "#222")}">{h(lv)}</span>'

    def _payload_summary(p: dict[str, Any]) -> str:
        try:
            s = json.dumps(p, default=str)
        except Exception:
            s = repr(p)
        if len(s) > 160:
            s = s[:157] + "…"
        return f"<code>{h(s)}</code>"

    if agents:
        agent_rows = "\n".join(
            (
                "<tr>"
                f"<td><b>{h(a['name'])}</b></td>"
                f"<td>{_kind(a['kind'])}</td>"
                f"<td>{h(a.get('deploy_target') or '—')}</td>"
                f"<td>{h(a.get('version') or '—')}</td>"
                f"<td title=\"{h(a.get('last_seen_at') or '')}\">{h(_fmt_age(a.get('last_seen_at')))}</td>"
                f"<td>{h(((a.get('last_run') or {}) or {}).get('kind') or '—')}</td>"
                f"<td>{_status(((a.get('last_run') or {}) or {}).get('status') or '—')}</td>"
                f"<td>{a.get('open_intents', 0)}</td>"
                "</tr>"
            )
            for a in agents
        )
    else:
        agent_rows = '<tr><td colspan="8"><i>no agents registered yet</i></td></tr>'

    if events:
        event_rows = "\n".join(
            (
                "<tr>"
                f"<td title=\"{h(e.get('ts') or '')}\">{h(_fmt_age(e.get('ts')))}</td>"
                f"<td>{h(e.get('agent_name') or '—')}</td>"
                f"<td><code>{h(e.get('kind') or '—')}</code></td>"
                f"<td>{_level(e.get('level') or 'info')}</td>"
                f"<td>{_payload_summary(e.get('payload') or {})}</td>"
                "</tr>"
            )
            for e in events
        )
    else:
        event_rows = '<tr><td colspan="5"><i>no events in last 60 min</i></td></tr>'

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    return f"""<!DOCTYPE html>
<html><head>
  <title>motto fleet</title>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="30">
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 1100px; margin: 24px auto; padding: 0 16px; color: #222; }}
    h1 {{ font-weight: 600; margin: 0; }}
    h2 {{ font-weight: 600; margin-top: 1.8em; margin-bottom: 0.4em; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ text-align: left; padding: 6px 8px; border-bottom: 2px solid #ddd; font-weight: 600; }}
    td {{ padding: 5px 8px; border-bottom: 1px solid #eee; vertical-align: top; }}
    code {{ font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px; }}
    .meta {{ color: #888; font-size: 12px; margin-top: 0.3em; }}
  </style>
</head><body>
  <h1>motto fleet</h1>
  <p class="meta">refreshes every 30s · {len(agents)} agents · {len(events)} recent events · {h(now)}</p>

  <h2>agents</h2>
  <table>
    <tr><th>name</th><th>kind</th><th>deploy</th><th>version</th><th>last seen</th><th>last run</th><th>status</th><th>open intents</th></tr>
    {agent_rows}
  </table>

  <h2>recent events (last 60 min)</h2>
  <table>
    <tr><th>when</th><th>agent</th><th>kind</th><th>level</th><th>payload</th></tr>
    {event_rows}
  </table>
</body></html>"""


# Cockpit routes (chat with director + intent submit + live state)
_register_cockpit_routes(mcp, db)


def main() -> None:
    logging.basicConfig(level=logging.INFO)

    # Mount domain servers when requested (e.g. all-in-one cluster deployment).
    if os.environ.get("MOTTO_MCP_MOUNT_DOMAIN_SERVERS") == "1":
        from servers.grabber.server import mcp as grabber_mcp  # noqa: PLC0415
        mcp.mount(grabber_mcp, namespace="grabber")

    port = int(os.environ.get("PORT", "8000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
