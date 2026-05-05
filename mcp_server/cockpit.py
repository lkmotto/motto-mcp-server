"""Motto Cockpit — centralized control UI for the agent fleet.

Routes:
    /cockpit                    Single-page UI (HTML)
    /cockpit/state.json         Live state for polling (auth)
    /cockpit/chat               POST: chat with director (Claude Max OAuth)
    /cockpit/intent             POST: submit a manual intent / nudge

The chat endpoint uses CLAUDE_CODE_OAUTH_TOKEN (the user's $200/mo Claude
Max subscription). The server-side prompt context includes live fleet
state so the director can answer questions and propose actions.

Submitting an intent inserts into fleet.intents with source='cockpit-user',
so motto-director will pick it up on the next consume_open_intents call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from html import escape as h
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .db import Database

logger = logging.getLogger(__name__)


# ── Auth ──────────────────────────────────────────────────────────────────────


def cockpit_auth_ok(request: Request) -> bool:
    """Same auth rule as /dashboard — Bearer header or ?token query."""
    expected = os.environ.get("MOTTO_MCP_AUTH_TOKEN")
    if not expected:
        return True
    if request.headers.get("authorization", "") == f"Bearer {expected}":
        return True
    if request.query_params.get("token") == expected:
        return True
    return False


def _unauth_html() -> HTMLResponse:
    return HTMLResponse(
        "<h1>unauthorized</h1>"
        "<p>pass <code>?token=&lt;MOTTO_MCP_AUTH_TOKEN&gt;</code> "
        "or <code>Authorization: Bearer &lt;token&gt;</code>.</p>",
        status_code=401,
    )


# ── Claude Max OAuth chat ─────────────────────────────────────────────────────


CLAUDE_CODE_SYSTEM_PREFIX = (
    "You are Claude Code, Anthropic's official CLI for Claude.\n\n"
)

DIRECTOR_PERSONA = (
    "You are the Motto Director — the orchestrator brain of Luke Motto's "
    "AI agent fleet. You speak conversationally and concisely, like a "
    "trusted technical co-founder. You have access to live fleet state "
    "(provided below) and can propose concrete next moves.\n\n"
    "When the user asks about fleet status, reference the data shown. "
    "When they propose work, give a crisp plan. When they nudge an agent, "
    "summarize what you'd tell the agent and suggest the user submit "
    "that as an intent. Never invent fleet data — if something isn't in "
    "the context, say so.\n\n"
    "## Local bridge\n\n"
    "Luke has a 'local bridge' panel in the cockpit: a queue of tasks the "
    "laptop runner picks up. Supported kinds: echo, shell, read_file, "
    "write_file, screenshot, ocr, claude_code (spawns Claude Code locally "
    "so quota comes from his Claude Max session, not the deployed OAuth "
    "token), browser. When something is best done on his machine — e.g. "
    "reviewing comp picks in his appraisal pipeline, reading a local file, "
    "capturing the screen, or spawning a Claude Code session in a repo — "
    "propose the exact JSON payload he should paste into that panel. "
    "Format suggestions as a fenced ```json block so they're easy to copy. "
    "Doctrine reminder: never auto-send emails to AMCs from any agent.\n"
)


async def _build_fleet_context(db: Database) -> str:
    """Snapshot of live fleet state, injected into the director's system prompt."""
    try:
        agents = await db.fleet_status()
        events = await db.recent_events(
            since_minutes=60, agent_name=None, kind=None, limit=30
        )
        runs = await db.list_runs(
            agent_name=None, status=None, since_minutes=60 * 24, limit=10
        )
    except Exception as e:  # pragma: no cover
        logger.exception("fleet context build failed")
        return f"[fleet state unavailable: {e}]"

    parts = ["# Live fleet state\n"]
    parts.append(f"Time: {datetime.now(timezone.utc).isoformat(timespec='seconds')}\n")
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
    parts.append(f"\n## Recent runs (last 24h, max 10)\n")
    for r in runs:
        parts.append(
            f"- {r.get('agent_name')} {r.get('kind')} {r.get('status')} "
            f"started={r.get('started_at')} summary={json.dumps(r.get('summary') or {}, default=str)[:200]}\n"
        )
    parts.append(f"\n## Recent events (last 60min, max 30)\n")
    for e in events:
        payload = json.dumps(e.get("payload") or {}, default=str)
        if len(payload) > 200:
            payload = payload[:197] + "…"
        parts.append(
            f"- {e.get('ts')} {e.get('agent_name')} {e.get('kind')} {payload}\n"
        )
    return "".join(parts)


async def call_claude_max(
    system: str,
    messages: list[dict[str, str]],
    model: str = "claude-sonnet-4-5",
    max_tokens: int = 1024,  # accepted for API parity; not passed to CLI
) -> dict[str, Any]:
    """Run the Claude Code CLI as a subprocess.

    Anthropic blocked direct OAuth POSTs to /v1/messages on 2026-01-09; the
    supported integration path for Claude Max-billed inference is the
    Claude Code CLI. This shim runs:

        claude -p <user> --output-format json --max-turns 1 \
               --model <model> --append-system-prompt <system>

    Critical: --append-system-prompt (NOT --system-prompt). The latter
    strips Claude Code's identity prefix and silently bills the API
    instead of Claude Max.

    CLAUDE_CODE_OAUTH_TOKEN must be set in the process env; the CLI
    picks it up automatically.

    `messages` is collapsed into a single transcript on the user side
    of the prompt because --max-turns 1 is non-interactive. We keep
    the full chat history visible to the model so it can answer
    follow-ups in context.

    Returns a dict shaped like the old /v1/messages response so callers
    (and `_extract_text`) don't need to change:
        success  -> {type: "message", content: [{type: "text", text: ...}],
                     model, usage, ...}
        error    -> {error: "...", type: "config_error"|"upstream_error"|...}
    """
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        return {
            "error": "CLAUDE_CODE_OAUTH_TOKEN not set on server",
            "type": "config_error",
        }

    cli_bin = os.environ.get("CLAUDE_CLI_BIN", "claude")
    if shutil.which(cli_bin) is None:
        return {
            "error": f"`{cli_bin}` CLI not on PATH inside the container",
            "type": "config_error",
        }

    # Collapse the history into a transcript. The last user turn is the
    # actual question; earlier turns become context above it.
    if not messages:
        return {"error": "no messages", "type": "config_error"}

    last = messages[-1]
    if last.get("role") != "user":
        return {
            "error": "last message must be from the user",
            "type": "config_error",
        }

    transcript_lines: list[str] = []
    for m in messages[:-1]:
        role = m.get("role", "user").upper()
        content = m.get("content", "")
        transcript_lines.append(f"[{role}]\n{content}")
    if transcript_lines:
        user_prompt = (
            "# Prior conversation\n"
            + "\n\n".join(transcript_lines)
            + "\n\n# Current message\n"
            + last.get("content", "")
        )
    else:
        user_prompt = last.get("content", "")

    # We deliberately do NOT prepend CLAUDE_CODE_SYSTEM_PREFIX here — the
    # CLI already starts every conversation with its Claude Code identity
    # prompt, and --append-system-prompt adds ours after it.
    cmd = [
        cli_bin,
        "-p", user_prompt,
        "--output-format", "json",
        "--max-turns", "1",
        "--model", model,
        "--append-system-prompt", system,
        # Northflank containers run as root. The Claude Code CLI refuses to
        # operate non-interactively under root unless this flag is set
        # (see anthropics/claude-code#3490, #2951, #9184). Safe under
        # `--max-turns 1` because no tool calls happen.
        "--dangerously-skip-permissions",
    ]

    timeout_s = float(os.environ.get("CLAUDE_MAX_TIMEOUT_S", "120"))

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": token, "IS_SANDBOX": "1"},
            # stdin pinned to DEVNULL: the Claude Code CLI inspects stdin
            # even when `-p <text>` provides the prompt and pauses ~3s on a
            # connected pipe. In a Northflank container the inherited stdin
            # is a closed/orphan TTY, which produces:
            #   "no stdin data received in 3s, proceeding without it"
            # followed by exit=1. Pinning to DEVNULL is the documented fix.
            # (Reproduced in motto-director run 3dd3a015 on 2026-05-05.)
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return {
                "error": f"claude CLI timed out after {timeout_s}s",
                "type": "timeout_error",
            }
    except FileNotFoundError as exc:
        return {"error": str(exc), "type": "config_error"}
    except Exception as e:  # pragma: no cover
        logger.exception("claude_max subprocess launch failed")
        return {"error": str(e), "type": "transport_error"}

    if proc.returncode != 0:
        stderr_s = (stderr_b or b"").decode("utf-8", "replace")[:1000]
        return {
            "error": f"claude CLI exited {proc.returncode}",
            "type": "upstream_error",
            "status_code": 502,
            "detail": stderr_s,
        }

    stdout_s = (stdout_b or b"").decode("utf-8", "replace").strip()
    if not stdout_s:
        return {
            "error": "claude CLI returned empty stdout",
            "type": "upstream_error",
            "status_code": 502,
        }

    try:
        envelope = json.loads(stdout_s)
    except json.JSONDecodeError as exc:
        return {
            "error": f"could not parse claude CLI JSON: {exc}",
            "type": "upstream_error",
            "status_code": 502,
            "detail": stdout_s[:500],
        }

    # The CLI envelope is shaped like:
    #   {"type":"result","subtype":"success","result":"<text>",
    #    "session_id":"...","usage":{...},"total_cost_usd":...,"model":"..."}
    if isinstance(envelope, dict) and envelope.get("subtype") == "success":
        text = envelope.get("result") or ""
        return {
            "type": "message",
            "content": [{"type": "text", "text": text}],
            "model": envelope.get("model") or model,
            "usage": envelope.get("usage") or {},
            "session_id": envelope.get("session_id"),
            "total_cost_usd": envelope.get("total_cost_usd"),
        }

    # Some envelopes use "is_error" or surface the failure differently.
    err_msg = (
        envelope.get("error")
        if isinstance(envelope, dict)
        else None
    ) or stdout_s[:300]
    return {
        "error": f"claude CLI returned non-success: {err_msg}",
        "type": "upstream_error",
        "status_code": 502,
        "detail": envelope,
    }


def _extract_text(resp: dict[str, Any]) -> str:
    """Pull the text content out of a Claude messages response."""
    if resp.get("type") == "error" or "error" in resp:
        err = resp.get("error") or resp
        if isinstance(err, dict):
            return f"[error: {err.get('message') or err.get('type') or json.dumps(err)[:300]}]"
        return f"[error: {err}]"
    blocks = resp.get("content") or []
    out = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text", ""))
    return "".join(out) if out else "[no text in response]"


# ── HTTP routes ───────────────────────────────────────────────────────────────


def register_routes(mcp, db: Database) -> None:
    """Attach cockpit routes to the FastMCP app."""

    @mcp.custom_route("/cockpit", methods=["GET"])
    async def cockpit_ui(request: Request):
        if not cockpit_auth_ok(request):
            return _unauth_html()
        token = request.query_params.get("token", "")
        return HTMLResponse(_render_cockpit(token))

    @mcp.custom_route("/cockpit/state.json", methods=["GET"])
    async def cockpit_state(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        agents = await db.fleet_status()
        events = await db.recent_events(
            since_minutes=60, agent_name=None, kind=None, limit=50
        )
        runs = await db.list_runs(
            agent_name=None, status=None, since_minutes=60 * 24, limit=10
        )
        return JSONResponse(
            {
                "now": datetime.now(timezone.utc).isoformat(),
                "agents": agents,
                "recent_events": events,
                "recent_runs": runs,
            }
        )

    @mcp.custom_route("/cockpit/chat", methods=["POST"])
    async def cockpit_chat(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        history: list[dict[str, str]] = body.get("messages") or []
        # Validate messages
        clean: list[dict[str, str]] = []
        for m in history:
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            content = m.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content:
                clean.append({"role": role, "content": content})
        if not clean:
            return JSONResponse({"error": "no messages"}, status_code=400)

        fleet_ctx = await _build_fleet_context(db)
        system = DIRECTOR_PERSONA + "\n" + fleet_ctx
        resp = await call_claude_max(
            system=system,
            messages=clean,
            max_tokens=int(body.get("max_tokens") or 1024),
        )
        text = _extract_text(resp)
        return JSONResponse(
            {
                "reply": text,
                "raw_type": resp.get("type"),
                "model": resp.get("model"),
                "usage": resp.get("usage"),
                "error": resp.get("error"),
            }
        )

    # ── Local-task HTTP endpoints (used by motto-local laptop runner) ──
    # The runner polls /local/claim, executes tasks, then POSTs /local/complete.
    # Same auth as everything else: ?token= or Bearer.

    @mcp.custom_route("/local/claim", methods=["POST"])
    async def local_claim(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}
        runner_id = body.get("runner_id") or "motto-local"
        kinds = body.get("kinds")
        limit = int(body.get("limit") or 5)
        tasks = await db.claim_local_tasks(
            runner_id=str(runner_id),
            kinds=kinds if isinstance(kinds, list) else None,
            limit=limit,
        )
        return JSONResponse({"tasks": tasks})

    @mcp.custom_route("/local/complete", methods=["POST"])
    async def local_complete(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        task_id = body.get("task_id")
        status = body.get("status")
        if not task_id or status not in ("succeeded", "failed", "cancelled"):
            return JSONResponse(
                {"error": "task_id + status (succeeded|failed|cancelled) required"},
                status_code=400,
            )
        try:
            ok = await db.complete_local_task(
                task_id=str(task_id),
                status=str(status),
                result=body.get("result"),
                error=body.get("error"),
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": ok})

    @mcp.custom_route("/local/queue", methods=["POST"])
    async def local_queue(request: Request):
        """Cockpit UI / cloud agents call this to queue a task."""
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        kind = body.get("kind")
        payload = body.get("payload") or {}
        if not kind or not isinstance(payload, dict):
            return JSONResponse(
                {"error": "kind + payload (object) required"}, status_code=400
            )
        try:
            row = await db.queue_local_task(
                kind=str(kind),
                payload=payload,
                source=str(body.get("source") or "cockpit-user"),
                description=body.get("description"),
                dedup_key=body.get("dedup_key"),
                ttl_seconds=int(body.get("ttl_seconds") or 600),
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse(row)

    @mcp.custom_route("/local/tasks.json", methods=["GET"])
    async def local_tasks_json(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        status = request.query_params.get("status")
        kind = request.query_params.get("kind")
        limit = int(request.query_params.get("limit") or 50)
        tasks = await db.list_local_tasks(status=status, kind=kind, limit=limit)
        return JSONResponse({"tasks": tasks})

    @mcp.custom_route("/local/runner-heartbeat", methods=["POST"])
    async def local_runner_heartbeat(request: Request):
        """Lightweight HTTP shim so the laptop runner can self-register +
        heartbeat into fleet.agents without speaking MCP. Body:
          {"agent_name":"motto-local-<host>", "status":{"queue_depth":0,...}}
        """
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        agent_name = body.get("agent_name")
        if not agent_name or not isinstance(agent_name, str):
            return JSONResponse({"error": "agent_name required"}, status_code=400)
        try:
            await db.upsert_agent(
                name=agent_name,
                kind="deterministic",
                deploy_target="local-laptop",
                version=str(body.get("version") or "motto-local/1"),
            )
            await db.heartbeat(
                agent_name=agent_name,
                status=body.get("status") or {},
            )
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/local/task/{task_id}", methods=["GET"])
    async def local_task_one(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        task_id = request.path_params["task_id"]
        task = await db.get_local_task(task_id=task_id)
        if not task:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse(task)

    # ── Long-polling endpoints ────────────────────────────────────────────────

    _LONGPOLL_MAX_S_DEFAULT: int = int(os.environ.get("LOCAL_LONGPOLL_MAX_S", "25"))
    _LONGPOLL_CAP_S: int = 60
    _LONGPOLL_POLL_INTERVAL_S: float = 0.2
    _TERMINAL_STATUSES: frozenset[str] = frozenset({"succeeded", "failed", "cancelled"})

    @mcp.custom_route("/local/claim/long-poll", methods=["POST"])
    async def local_claim_long_poll(request: Request):
        """Like POST /local/claim but holds the connection open until at least
        one task is claimable or the deadline elapses (returns empty list).

        Query params:
            max_wait_s  Override wait duration (capped at 60 s).
        """
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            body = {}

        runner_id = body.get("runner_id") or "motto-local"
        kinds = body.get("kinds")
        limit = int(body.get("limit") or 5)

        raw_max = request.query_params.get("max_wait_s")
        max_wait_s = min(
            float(raw_max) if raw_max is not None else _LONGPOLL_MAX_S_DEFAULT,
            _LONGPOLL_CAP_S,
        )

        deadline = asyncio.get_event_loop().time() + max_wait_s

        while True:
            # Bail early if client disconnected.
            if await request.is_disconnected():
                return JSONResponse({"tasks": []})

            tasks = await db.claim_local_tasks(
                runner_id=str(runner_id),
                kinds=kinds if isinstance(kinds, list) else None,
                limit=limit,
            )
            if tasks:
                return JSONResponse({"tasks": tasks})

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return JSONResponse({"tasks": []})

            await asyncio.sleep(min(_LONGPOLL_POLL_INTERVAL_S, remaining))

    @mcp.custom_route("/local/task/{task_id}/wait", methods=["GET"])
    async def local_task_wait(request: Request):
        """Like GET /local/task/{task_id} but holds the connection open until
        the task reaches a terminal status (succeeded/failed/cancelled) or the
        deadline elapses.  If the task is already terminal, returns immediately.

        Query params:
            max_wait_s  Override wait duration (capped at 60 s).
        """
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        task_id = request.path_params["task_id"]

        raw_max = request.query_params.get("max_wait_s")
        max_wait_s = min(
            float(raw_max) if raw_max is not None else _LONGPOLL_MAX_S_DEFAULT,
            _LONGPOLL_CAP_S,
        )

        deadline = asyncio.get_event_loop().time() + max_wait_s

        while True:
            # Bail early if client disconnected.
            if await request.is_disconnected():
                task = await db.get_local_task(task_id=task_id)
                if task:
                    return JSONResponse(task)
                return JSONResponse({"error": "not found"}, status_code=404)

            task = await db.get_local_task(task_id=task_id)
            if not task:
                return JSONResponse({"error": "not found"}, status_code=404)

            if task.get("status") in _TERMINAL_STATUSES:
                return JSONResponse(task)

            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return JSONResponse(task)

            await asyncio.sleep(min(_LONGPOLL_POLL_INTERVAL_S, remaining))

    @mcp.custom_route("/cockpit/intent", methods=["POST"])
    async def cockpit_intent(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        target_agent = body.get("target_agent")
        kind = body.get("kind")
        payload = body.get("payload") or {}
        if not target_agent or not kind:
            return JSONResponse(
                {"error": "target_agent and kind required"}, status_code=400
            )
        if not isinstance(payload, dict):
            return JSONResponse({"error": "payload must be object"}, status_code=400)

        try:
            intent_id = await db.signal_intent(
                target_agent=str(target_agent),
                kind=str(kind),
                payload=payload,
                source_agent="cockpit-user",
            )
        except Exception as e:
            logger.exception("intent submit failed")
            return JSONResponse({"error": str(e)}, status_code=500)

        return JSONResponse({"intent_id": str(intent_id), "ok": True})


# ── Single-page UI ────────────────────────────────────────────────────────────


def _render_cockpit(token: str) -> str:
    """Single-page cockpit UI. Token is embedded so the same browser session
    can call /cockpit/* JSON endpoints from JS without re-prompting.
    """
    safe_token = h(token)
    return f"""<!DOCTYPE html>
<html><head>
  <title>motto cockpit</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {{
      --bg: #0e1116; --panel: #161b22; --border: #2d333b;
      --fg: #e6edf3; --muted: #7d8590; --accent: #2f81f7;
      --ok: #3fb950; --warn: #d29922; --err: #f85149;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg); color: var(--fg); font-size: 14px; min-height: 100vh;
    }}
    .top {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 16px; border-bottom: 1px solid var(--border); background: var(--panel);
    }}
    .top h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
    .top .meta {{ color: var(--muted); font-size: 12px; }}
    .layout {{
      display: grid; grid-template-columns: 1.2fr 1fr; gap: 12px;
      padding: 12px; height: calc(100vh - 49px);
    }}
    @media (max-width: 1000px) {{
      .layout {{ grid-template-columns: 1fr; height: auto; }}
    }}
    .col {{ display: flex; flex-direction: column; gap: 12px; min-height: 0; }}
    .panel {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 6px; display: flex; flex-direction: column; min-height: 0;
    }}
    .panel h2 {{
      margin: 0; padding: 10px 14px; font-size: 13px; font-weight: 600;
      border-bottom: 1px solid var(--border); color: var(--muted);
      text-transform: uppercase; letter-spacing: 0.5px;
      display: flex; justify-content: space-between; align-items: center;
    }}
    .panel .body {{ padding: 12px 14px; overflow-y: auto; flex: 1; min-height: 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
    th, td {{
      text-align: left; padding: 4px 6px; border-bottom: 1px solid var(--border);
      vertical-align: top;
    }}
    th {{ color: var(--muted); font-weight: 600; font-size: 11px; }}
    code {{ font-family: ui-monospace, monospace; font-size: 11px; color: var(--fg); }}
    .ok {{ color: var(--ok); }}
    .warn {{ color: var(--warn); }}
    .err {{ color: var(--err); }}
    .accent {{ color: var(--accent); }}

    /* chat */
    #chat-log {{
      flex: 1; overflow-y: auto; padding: 12px 14px;
      display: flex; flex-direction: column; gap: 8px;
    }}
    .msg {{ padding: 8px 10px; border-radius: 6px; max-width: 85%; line-height: 1.45; white-space: pre-wrap; word-wrap: break-word; }}
    .msg.user {{ background: #1f6feb33; border: 1px solid #1f6feb55; align-self: flex-end; }}
    .msg.assistant {{ background: #161b22; border: 1px solid var(--border); align-self: flex-start; }}
    .msg.thinking {{ color: var(--muted); font-style: italic; }}
    .msg.error {{ background: #f8514922; border: 1px solid var(--err); color: var(--err); }}
    #chat-form {{
      display: flex; gap: 8px; padding: 10px; border-top: 1px solid var(--border);
    }}
    #chat-input {{
      flex: 1; padding: 8px 10px; background: var(--bg); border: 1px solid var(--border);
      border-radius: 4px; color: var(--fg); font-size: 13px; resize: vertical; min-height: 38px; max-height: 120px;
      font-family: inherit;
    }}
    button {{
      background: var(--accent); color: white; border: 0; padding: 8px 14px;
      border-radius: 4px; cursor: pointer; font-size: 13px; font-weight: 500;
    }}
    button:hover {{ background: #4493f8; }}
    button:disabled {{ background: #555; cursor: not-allowed; }}
    button.ghost {{ background: transparent; color: var(--accent); border: 1px solid var(--accent); }}
    button.ghost:hover {{ background: #2f81f722; }}

    /* intent form */
    #intent-form {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }}
    #intent-form input, #intent-form textarea {{
      background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
      padding: 6px 8px; color: var(--fg); font-size: 12px; font-family: inherit;
    }}
    #intent-form textarea {{ grid-column: 1 / -1; min-height: 60px; resize: vertical; }}
    #intent-form .row-full {{ grid-column: 1 / -1; display: flex; gap: 8px; align-items: center; }}
    .quick-btns {{ display: flex; flex-wrap: wrap; gap: 6px; padding: 8px 14px 12px; }}
    .quick-btns button {{ font-size: 11px; padding: 4px 10px; }}
    #intent-result {{ font-size: 12px; padding: 0 14px 12px; }}
  </style>
</head><body>
  <div class="top">
    <h1>🛰️ motto cockpit</h1>
    <span class="meta" id="status-line">connecting…</span>
  </div>

  <div class="layout">
    <!-- Left: chat -->
    <div class="col">
      <div class="panel" style="flex:1">
        <h2>director chat <span style="font-weight:400;font-size:11px">claude max · sonnet 4.5</span></h2>
        <div id="chat-log"></div>
        <form id="chat-form">
          <textarea id="chat-input" placeholder="ask the director… (enter to send, shift+enter for newline)"></textarea>
          <button type="submit" id="chat-send">send</button>
        </form>
      </div>
    </div>

    <!-- Right: fleet + intent -->
    <div class="col">
      <div class="panel">
        <h2>fleet <span id="agent-count" style="font-weight:400">—</span></h2>
        <div class="body" id="agents-body">loading…</div>
      </div>

      <div class="panel">
        <h2>send intent <span style="font-weight:400;font-size:11px">queues a manual nudge for an agent</span></h2>
        <form id="intent-form" style="padding:10px 14px;">
          <input id="i-target" placeholder="target_agent (e.g. motto-director)" required>
          <input id="i-kind" placeholder="kind (e.g. focus, halt, retry-pr)" required>
          <textarea id="i-payload" placeholder='payload JSON (e.g. {{"pr":42,"reason":"flaky test"}})'></textarea>
          <div class="row-full">
            <button type="submit">queue intent</button>
            <button type="button" class="ghost" id="i-clear">clear</button>
            <span id="intent-result"></span>
          </div>
        </form>
        <div class="quick-btns">
          <button class="ghost" data-target="motto-director" data-kind="poll-now">poll director now</button>
          <button class="ghost" data-target="motto-director" data-kind="merge-greenlit">merge greenlit PRs</button>
          <button class="ghost" data-target="motto-sdr-agent" data-kind="dry-run">SDR dry-run</button>
        </div>
      </div>

      <div class="panel">
        <h2>local bridge <span id="local-status" style="font-weight:400;font-size:11px">no runner detected</span></h2>
        <form id="local-form" style="padding:10px 14px;">
          <select id="l-kind" style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--fg);font-size:12px;width:100%;margin-bottom:6px;">
            <option value="echo">echo · sanity ping</option>
            <option value="shell">shell · run a command</option>
            <option value="read_file">read_file · path</option>
            <option value="write_file">write_file · path + content</option>
            <option value="screenshot">screenshot · capture screen</option>
            <option value="ocr">ocr · path → text</option>
            <option value="claude_code">claude_code · prompt</option>
            <option value="browser">browser · url + action</option>
          </select>
          <textarea id="l-payload" placeholder='payload JSON (e.g. {{"cmd":"ls -la"}})' style="background:var(--bg);border:1px solid var(--border);border-radius:4px;padding:6px 8px;color:var(--fg);font-size:12px;width:100%;min-height:50px;font-family:inherit;"></textarea>
          <div style="display:flex;gap:8px;align-items:center;margin-top:6px;">
            <button type="submit">queue local task</button>
            <span id="local-result" style="font-size:12px;"></span>
          </div>
        </form>
        <div class="body" id="local-body" style="max-height:240px">no tasks yet</div>
      </div>

      <div class="panel" style="flex:1">
        <h2>recent events <span style="font-weight:400;font-size:11px">last 60 min</span></h2>
        <div class="body" id="events-body">loading…</div>
      </div>
    </div>
  </div>

<script>
const TOKEN = "{safe_token}";
const Q = TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "";

let chatHistory = [];

function fmtAge(ts) {{
  if (!ts) return "—";
  const dt = new Date(ts);
  const s = (Date.now() - dt.getTime()) / 1000;
  if (s < 0) return "now";
  if (s < 60) return Math.floor(s) + "s";
  if (s < 3600) return Math.floor(s/60) + "m";
  if (s < 86400) return Math.floor(s/3600) + "h";
  return Math.floor(s/86400) + "d";
}}

function statusColor(s) {{
  if (s === "success") return "ok";
  if (s === "error") return "err";
  if (s === "running") return "accent";
  return "";
}}

async function refreshState() {{
  try {{
    const r = await fetch("/cockpit/state.json" + Q);
    if (!r.ok) {{ document.getElementById("status-line").textContent = "auth error"; return; }}
    const d = await r.json();

    // Status line
    document.getElementById("status-line").textContent =
      d.agents.length + " agents · " + d.recent_events.length + " events · updated " + new Date().toLocaleTimeString();
    document.getElementById("agent-count").textContent = d.agents.length;

    // Agents table
    const aBody = document.getElementById("agents-body");
    if (!d.agents.length) {{
      aBody.innerHTML = "<i>no agents registered</i>";
    }} else {{
      let html = "<table><tr><th>agent</th><th>kind</th><th>last seen</th><th>last run</th><th>open</th></tr>";
      for (const a of d.agents) {{
        const lr = a.last_run || {{}};
        html += "<tr>" +
          "<td><b>" + escapeHtml(a.name) + "</b></td>" +
          "<td>" + escapeHtml(a.kind) + "</td>" +
          "<td title='" + escapeHtml(a.last_seen_at || "") + "'>" + fmtAge(a.last_seen_at) + "</td>" +
          "<td><span class='" + statusColor(lr.status) + "'>" + escapeHtml(lr.kind || "—") + " " + escapeHtml(lr.status || "") + "</span></td>" +
          "<td>" + (a.open_intents || 0) + "</td>" +
          "</tr>";
      }}
      html += "</table>";
      aBody.innerHTML = html;
    }}

    // Events
    const eBody = document.getElementById("events-body");
    if (!d.recent_events.length) {{
      eBody.innerHTML = "<i>no events</i>";
    }} else {{
      let html = "<table><tr><th>when</th><th>agent</th><th>kind</th><th>payload</th></tr>";
      for (const e of d.recent_events.slice(0, 30)) {{
        const p = JSON.stringify(e.payload || {{}});
        const pShort = p.length > 100 ? p.slice(0, 97) + "…" : p;
        html += "<tr>" +
          "<td title='" + escapeHtml(e.ts || "") + "'>" + fmtAge(e.ts) + "</td>" +
          "<td>" + escapeHtml(e.agent_name || "—") + "</td>" +
          "<td><code>" + escapeHtml(e.kind || "—") + "</code></td>" +
          "<td><code>" + escapeHtml(pShort) + "</code></td>" +
          "</tr>";
      }}
      html += "</table>";
      eBody.innerHTML = html;
    }}
  }} catch (err) {{
    document.getElementById("status-line").textContent = "error: " + err.message;
  }}
}}

function escapeHtml(s) {{
  if (s == null) return "";
  return String(s).replace(/[&<>"']/g, c => ({{
    "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
  }})[c]);
}}

function addChatMsg(role, text, cls) {{
  const log = document.getElementById("chat-log");
  const div = document.createElement("div");
  div.className = "msg " + role + (cls ? " " + cls : "");
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}}

document.getElementById("chat-form").addEventListener("submit", async (ev) => {{
  ev.preventDefault();
  const input = document.getElementById("chat-input");
  const sendBtn = document.getElementById("chat-send");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addChatMsg("user", text);
  chatHistory.push({{role:"user", content:text}});
  const thinking = addChatMsg("assistant", "thinking…", "thinking");
  sendBtn.disabled = true;
  try {{
    const r = await fetch("/cockpit/chat" + Q, {{
      method: "POST",
      headers: {{"content-type":"application/json"}},
      body: JSON.stringify({{messages: chatHistory}})
    }});
    const d = await r.json();
    thinking.remove();
    if (d.error || (d.reply && d.reply.startsWith("[error"))) {{
      const msg = d.reply || ("error: " + JSON.stringify(d.error));
      addChatMsg("assistant", msg, "error");
      // don't push errors to history
    }} else {{
      addChatMsg("assistant", d.reply);
      chatHistory.push({{role:"assistant", content:d.reply}});
    }}
  }} catch (err) {{
    thinking.remove();
    addChatMsg("assistant", "transport error: " + err.message, "error");
  }}
  sendBtn.disabled = false;
  input.focus();
}});

// Enter to send, Shift+Enter for newline
document.getElementById("chat-input").addEventListener("keydown", (ev) => {{
  if (ev.key === "Enter" && !ev.shiftKey) {{
    ev.preventDefault();
    document.getElementById("chat-form").dispatchEvent(new Event("submit"));
  }}
}});

// Intent form
document.getElementById("intent-form").addEventListener("submit", async (ev) => {{
  ev.preventDefault();
  const target = document.getElementById("i-target").value.trim();
  const kind = document.getElementById("i-kind").value.trim();
  const payloadRaw = document.getElementById("i-payload").value.trim();
  let payload = {{}};
  if (payloadRaw) {{
    try {{ payload = JSON.parse(payloadRaw); }}
    catch {{
      document.getElementById("intent-result").innerHTML = "<span class='err'>invalid JSON in payload</span>";
      return;
    }}
  }}
  document.getElementById("intent-result").textContent = "submitting…";
  try {{
    const r = await fetch("/cockpit/intent" + Q, {{
      method: "POST",
      headers: {{"content-type":"application/json"}},
      body: JSON.stringify({{target_agent: target, kind, payload}})
    }});
    const d = await r.json();
    if (d.ok) {{
      document.getElementById("intent-result").innerHTML =
        "<span class='ok'>queued · " + escapeHtml(d.intent_id.slice(0,8)) + "</span>";
      document.getElementById("i-payload").value = "";
      refreshState();
    }} else {{
      document.getElementById("intent-result").innerHTML =
        "<span class='err'>" + escapeHtml(d.error || "failed") + "</span>";
    }}
  }} catch (err) {{
    document.getElementById("intent-result").innerHTML =
      "<span class='err'>" + escapeHtml(err.message) + "</span>";
  }}
}});

document.getElementById("i-clear").addEventListener("click", () => {{
  document.getElementById("i-target").value = "";
  document.getElementById("i-kind").value = "";
  document.getElementById("i-payload").value = "";
  document.getElementById("intent-result").textContent = "";
}});

// Quick action buttons
document.querySelectorAll(".quick-btns button").forEach(btn => {{
  btn.addEventListener("click", () => {{
    document.getElementById("i-target").value = btn.dataset.target;
    document.getElementById("i-kind").value = btn.dataset.kind;
    document.getElementById("i-payload").value = "";
    document.getElementById("i-target").scrollIntoView({{behavior:"smooth"}});
  }});
}});

// ── Local bridge ─────────────────────────────────────────────
const LOCAL_PAYLOAD_HINTS = {{
  echo: '{{"msg":"hello from cockpit"}}',
  shell: '{{"cmd":"ls -la","cwd":"~"}}',
  read_file: '{{"path":"/Users/luke/something.txt"}}',
  write_file: '{{"path":"/tmp/test.txt","content":"hello"}}',
  screenshot: '{{}}',
  ocr: '{{"path":"/tmp/screenshot.png"}}',
  claude_code: '{{"prompt":"review the comp picks in this appraisal","cwd":"~/projects/motto"}}',
  browser: '{{"url":"https://example.com","action":"screenshot"}}'
}};

document.getElementById("l-kind").addEventListener("change", (ev) => {{
  const ta = document.getElementById("l-payload");
  if (!ta.value.trim()) ta.value = LOCAL_PAYLOAD_HINTS[ev.target.value] || "{{}}";
}});
document.getElementById("l-payload").value = LOCAL_PAYLOAD_HINTS.echo;

async function refreshLocal() {{
  try {{
    const r = await fetch("/local/tasks.json" + (Q ? Q + "&" : "?") + "limit=15");
    if (!r.ok) return;
    const d = await r.json();
    const body = document.getElementById("local-body");
    const status = document.getElementById("local-status");
    if (!d.tasks || !d.tasks.length) {{
      body.innerHTML = "<i>no tasks yet</i>";
      status.textContent = "queue empty";
      return;
    }}
    const claimed = d.tasks.filter(t => t.claimed_by).map(t => t.claimed_by);
    const runners = [...new Set(claimed)];
    status.textContent = runners.length ? ("runner: " + runners.join(", ")) : "no runner has claimed yet";
    let html = "<table><tr><th>when</th><th>kind</th><th>status</th><th>desc</th></tr>";
    for (const t of d.tasks) {{
      const cls = t.status === "succeeded" ? "ok" : (t.status === "failed" ? "err" : (t.status === "running" || t.status === "claimed" ? "accent" : ""));
      const desc = t.description || (t.error ? t.error.slice(0, 60) : (t.kind + (t.claimed_by ? " → " + t.claimed_by : "")));
      html += "<tr>" +
        "<td title='" + escapeHtml(t.created_at || "") + "'>" + fmtAge(t.created_at) + "</td>" +
        "<td><code>" + escapeHtml(t.kind) + "</code></td>" +
        "<td><span class='" + cls + "'>" + escapeHtml(t.status) + "</span></td>" +
        "<td><code title='" + escapeHtml(t.id || "") + "'>" + escapeHtml(desc) + "</code></td>" +
        "</tr>";
    }}
    html += "</table>";
    body.innerHTML = html;
  }} catch (err) {{
    // silent
  }}
}}

document.getElementById("local-form").addEventListener("submit", async (ev) => {{
  ev.preventDefault();
  const kind = document.getElementById("l-kind").value;
  const payloadRaw = document.getElementById("l-payload").value.trim();
  let payload = {{}};
  if (payloadRaw) {{
    try {{ payload = JSON.parse(payloadRaw); }}
    catch {{
      document.getElementById("local-result").innerHTML = "<span class='err'>invalid JSON</span>";
      return;
    }}
  }}
  document.getElementById("local-result").textContent = "queueing…";
  try {{
    const r = await fetch("/local/queue" + Q, {{
      method: "POST",
      headers: {{"content-type":"application/json"}},
      body: JSON.stringify({{kind, payload, source: "cockpit-user"}})
    }});
    const d = await r.json();
    if (d.id) {{
      document.getElementById("local-result").innerHTML = "<span class='ok'>queued · " + escapeHtml(d.id.slice(0,8)) + "</span>";
      refreshLocal();
    }} else {{
      document.getElementById("local-result").innerHTML = "<span class='err'>" + escapeHtml(d.error || "failed") + "</span>";
    }}
  }} catch (err) {{
    document.getElementById("local-result").innerHTML = "<span class='err'>" + escapeHtml(err.message) + "</span>";
  }}
}});

// Initial load + poll
refreshState();
refreshLocal();
setInterval(refreshState, 15000);
setInterval(refreshLocal, 5000);

// Welcome
addChatMsg("assistant", "I'm the Motto Director. I can see live fleet state in my context. Ask me what's happening, what to do next, or describe a nudge you want to send.");
</script>
</body></html>"""
