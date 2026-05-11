"""Motto Cockpit — centralized control UI for the agent fleet.

Routes:
    /cockpit                    Single-page UI (HTML)
    /cockpit/state.json         Live state for polling (auth)
    /cockpit/chat               POST: chat with director (DeepSeek V4-Flash + tools)
    /cockpit/intent             POST: submit a manual intent / nudge

The chat endpoint uses DeepSeek's OpenAI-compatible API (key:
DEEPSEEK_API_KEY in motto-core-prd). The server-side prompt context
includes live fleet state. As of 2026-05-06 chat exposes a function-
calling palette (see chat_tools.py) so the director can run real reads
and file pending_moves through the same approval queue Luke already
uses. Manual approval mode (DIRECTOR_APPROVAL_MODE=manual) is preserved.

Provider: DeepSeek V4-Flash, single-provider, OpenAI-compatible,
1M context. Mirrors motto-director's deepseek-only chain. Earlier
Claude Max / Claude Code CLI shims are gone.

Submitting an intent inserts into fleet.intents with source='cockpit-user',
so motto-director will pick it up on the next consume_open_intents call.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from html import escape as h
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse

from .db import Database
from . import chat_tools

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


# ── Director chat (DeepSeek + tools) ──────────────────────────────────────────

DIRECTOR_PERSONA = (
    "You are the Motto Director \u2014 the orchestrator brain of Luke Motto's "
    "AI agent fleet. You speak conversationally and concisely, like a "
    "trusted technical co-founder. You have live fleet state in the system "
    "prompt and a set of TOOLS that let you act, not just talk.\n\n"
    "## Tools you have\n\n"
    "You can READ state and PROPOSE moves. Reads run inline. Proposals "
    "file rows into the cockpit's pending_moves queue with status='pending' "
    "\u2014 Luke approves them via the approval queue UI before they execute. "
    "You never bypass that gate.\n\n"
    "Reads: list_pending_moves, list_verifications, list_capability_requests, "
    "get_trust_scores, get_fleet_status. Use these instead of guessing.\n\n"
    "Write-intent: propose_verify_move (verify an applied move and update "
    "trust), propose_file_issue (track work in a repo), propose_noop (queue "
    "sanity ping). Anything destructive (merge_pr, spawn_session, compound_pr) "
    "is intentionally NOT in your palette \u2014 those flow through director's "
    "normal cycle.\n\n"
    "Capabilities: request_capability files a request when you need a "
    "resource (API key, OAuth scope, connector) you don't currently have. "
    "decide_capability_request grants/denies pending requests, but only "
    "when Luke explicitly says to.\n\n"
    "## How to operate\n\n"
    "1. When asked about state, call the read tool first. Don't speculate.\n"
    "2. When Luke gives a directive (\"verify move 97\", \"track this as an "
    "issue\", \"we need a github token\"), pick the right tool and call it. "
    "Echo the resulting queued_move_id or request_id back so he can act on "
    "it.\n"
    "3. After a tool call returns, summarize what happened in one or two "
    "sentences. Don't re-dump the raw payload.\n"
    "4. If a tool returns an error, surface it clearly and suggest a fix "
    "\u2014 don't silently retry.\n"
    "5. Never invent move IDs, repo names, capability names, or trust "
    "numbers. If you don't have a value, list_* it first.\n\n"
    "## Standing rules\n\n"
    "- Never auto-send emails to AMCs from any agent.\n"
    "- Manual approval mode is sacred. Filing a pending_move is fine; "
    "approving it is Luke's call.\n"
    "- Trust scores update only on definitive (passed/failed) verifications. "
    "Inconclusive and error don't move the needle.\n"
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


DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
)
DEEPSEEK_DEFAULT_MODEL = os.environ.get(
    "DEEPSEEK_MODEL", "deepseek-v4-flash"
)
DEEPSEEK_TIMEOUT_S = float(os.environ.get("DEEPSEEK_TIMEOUT_S", "120"))


async def call_deepseek(  # noqa: C901
    system: str,
    messages: list[dict[str, Any]],
    model: str | None = None,
    max_tokens: int = 1024,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call DeepSeek's OpenAI-compatible chat completions API.

    DEEPSEEK_API_KEY must be set in the process env (lives in motto-core-prd
    as of 2026-05-05; mirrored from the user's repo-level DEEPSEEK_API
    secret via .github/workflows/sync-deepseek-secret.yml in motto-director).

    `messages` is the conversation history. The system prompt is prepended
    as a `system` role message. When `tools` is provided, the model can
    return tool_calls in its response (OpenAI function-calling shape); the
    caller is responsible for executing them and re-prompting with the
    results.

    Returns a dict that always carries:
        type     : "message" on success, "error" on failure
        content  : [{type:"text", text}]   (assistant text, possibly empty)
        tool_calls: list of OpenAI tool_call objects when present
        finish_reason: stop | tool_calls | length | ...
        message  : raw OpenAI assistant message (echoed for re-injection)
        model, usage, id
        error    : on failure
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return {
            "error": "DEEPSEEK_API_KEY not set on server",
            "type": "config_error",
        }

    if not messages:
        return {"error": "no messages", "type": "config_error"}

    chosen_model = model or DEEPSEEK_DEFAULT_MODEL

    # OpenAI-compatible chat completions: prepend system as a role-message
    # rather than relying on a separate system parameter (DeepSeek accepts
    # both but role-message is more portable).
    payload_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system}
    ]
    for m in messages:
        role = m.get("role", "user")
        if role == "system":
            continue  # already injected
        if role == "tool":
            # Tool result message — must include tool_call_id + content (str)
            payload_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id"),
                    "content": m.get("content") or "",
                }
            )
            continue
        if role == "assistant":
            # Assistant turn — may carry tool_calls; preserve them so the
            # model sees its prior decisions. OpenAI/DeepSeek strict mode
            # requires content=null when tool_calls is set; '' is rejected.
            tcs = m.get("tool_calls")
            if tcs:
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": tcs,
                }
            else:
                assistant_msg = {
                    "role": "assistant",
                    "content": m.get("content") or "",
                }
            payload_messages.append(assistant_msg)
            continue
        if role == "user":
            content = m.get("content", "") or ""
            if content:
                payload_messages.append({"role": "user", "content": content})

    body: dict[str, Any] = {
        "model": chosen_model,
        "messages": payload_messages,
        "max_tokens": max_tokens,
        "stream": False,
    }
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"

    url = f"{DEEPSEEK_BASE_URL.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=DEEPSEEK_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.TimeoutException:
        return {
            "error": f"deepseek API timed out after {DEEPSEEK_TIMEOUT_S}s",
            "type": "timeout_error",
        }
    except httpx.HTTPError as exc:  # pragma: no cover
        logger.exception("deepseek transport error")
        return {"error": str(exc), "type": "transport_error"}

    if resp.status_code >= 400:
        # Surface the status + body so the caller can render a useful error.
        detail = resp.text[:1000] if resp.text else ""
        return {
            "error": f"deepseek HTTP {resp.status_code}",
            "type": "upstream_error",
            "status_code": resp.status_code,
            "detail": detail,
        }

    try:
        envelope = resp.json()
    except json.JSONDecodeError as exc:
        return {
            "error": f"could not parse deepseek JSON: {exc}",
            "type": "upstream_error",
            "status_code": 502,
            "detail": resp.text[:500],
        }

    # OpenAI-compatible envelope:
    #   {"id":..., "choices":[{"message":{"role":"assistant","content":...}, ...}],
    #    "usage":{"prompt_tokens":..., "completion_tokens":..., "total_tokens":...},
    #    "model":...}
    choices = envelope.get("choices") or []
    if not choices:
        return {
            "error": "deepseek returned no choices",
            "type": "upstream_error",
            "status_code": 502,
            "detail": envelope,
        }
    choice0 = choices[0] or {}
    msg = choice0.get("message") or {}
    text = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    finish_reason = choice0.get("finish_reason") or "stop"
    return {
        "type": "message",
        "content": [{"type": "text", "text": text}],
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "message": msg,  # raw assistant turn for re-injection
        "model": envelope.get("model") or chosen_model,
        "usage": envelope.get("usage") or {},
        "id": envelope.get("id"),
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


def register_routes(mcp, db: Database) -> None:  # noqa: C901
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
    async def cockpit_chat(request: Request):  # noqa: C901
        """Director chat with tool-calling.

        Loop: model -> (text, tool_calls). If tool_calls present, dispatch
        each one server-side, append assistant + tool messages to history,
        re-prompt. Stop when finish_reason='stop' or MAX_TOOL_HOPS hit.

        Response carries:
          reply         : final assistant text
          tool_calls    : flat list of every tool call run this turn
                          (name, arguments, result, ok). UI renders these
                          inline so Luke sees what the director did.
          hops          : how many model rounds we ran
          model, usage, error
        """
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)

        # Allow tools to be disabled per-request for debugging / regressions.
        tools_enabled = bool(body.get("tools_enabled", True))
        chat_user = str(body.get("chat_user") or "luke")
        max_tokens = int(body.get("max_tokens") or 1024)

        # Inbound history: accept user / assistant (str content). Older clients
        # don't send tool messages — we rebuild those server-side per turn.
        raw_history = body.get("messages") or []
        clean: list[dict[str, Any]] = []
        for m in raw_history:
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
        # Pull verify_move out of server.py at runtime to avoid an import
        # cycle (server imports cockpit, not the other way around).
        from . import server as _server
        verify_move_fn = getattr(_server, "verify_move")

        tools = chat_tools.TOOL_SCHEMAS if tools_enabled else None
        tool_log: list[dict[str, Any]] = []
        last_resp: dict[str, Any] = {}

        for hop in range(chat_tools.MAX_TOOL_HOPS):
            resp = await call_deepseek(
                system=system,
                messages=clean,
                max_tokens=max_tokens,
                tools=tools,
            )
            last_resp = resp
            if resp.get("error"):
                logger.warning(
                    "chat deepseek error hop=%d err=%s detail=%s",
                    hop,
                    resp.get("error"),
                    str(resp.get("detail"))[:500],
                )
                return JSONResponse(
                    {
                        "reply": _extract_text(resp),
                        "tool_calls": tool_log,
                        "hops": hop,
                        "error": resp.get("error"),
                        "detail": resp.get("detail"),
                        "model": resp.get("model"),
                        "usage": resp.get("usage"),
                    }
                )

            tool_calls = resp.get("tool_calls") or []
            finish_reason = resp.get("finish_reason") or "stop"

            if not tool_calls:
                break

            # Append assistant turn (with tool_calls) to history so the
            # model sees its own decisions on the next round.
            assistant_msg = resp.get("message") or {}
            clean.append(
                {
                    "role": "assistant",
                    "content": assistant_msg.get("content") or "",
                    "tool_calls": tool_calls,
                }
            )

            # Dispatch each tool call and append a `tool` role result.
            for tc in tool_calls:
                tc_id = tc.get("id")
                fn = (tc.get("function") or {})
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
                except json.JSONDecodeError:
                    args = {}
                result = await chat_tools.dispatch(
                    name=name,
                    arguments=args,
                    db=db,
                    chat_user=chat_user,
                    verify_move_fn=verify_move_fn,
                )
                tool_log.append(
                    {
                        "name": name,
                        "arguments": args,
                        "result": result,
                        "ok": not (isinstance(result, dict) and result.get("error")),
                        "hop": hop,
                    }
                )
                # Tool result content must be a string per OpenAI spec.
                clean.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": json.dumps(result, default=str)[:8000],
                    }
                )

            if finish_reason == "stop" and not tool_calls:
                break
        else:
            # Hit MAX_TOOL_HOPS — force a final summarization round
            # without tools to make sure the user gets text back.
            resp = await call_deepseek(
                system=system
                + "\n\n[note: tool budget exhausted, summarize what you did]",
                messages=clean,
                max_tokens=max_tokens,
                tools=None,
            )
            last_resp = resp

        text = _extract_text(last_resp)
        return JSONResponse(
            {
                "reply": text,
                "tool_calls": tool_log,
                "hops": len(
                    [tc for tc in tool_log if tc.get("hop") is not None]
                )
                and (max(tc.get("hop") for tc in tool_log) + 1)
                or 0,
                "raw_type": last_resp.get("type"),
                "model": last_resp.get("model"),
                "usage": last_resp.get("usage"),
                "error": last_resp.get("error"),
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

    # ── Director approval queue (motto-director PR #41) ─────────────────
    # Surfaces public.pending_moves rows so a human can approve / reject
    # before motto-director auto-acts. Same auth as everything else.

    def _approver_id(request: Request) -> str:
        token = (
            request.query_params.get("token")
            or request.headers.get("authorization", "").removeprefix("Bearer ")
        )
        # Don't echo the full token — a stable short fingerprint is enough.
        return f"cockpit:{token[:8]}" if token else "cockpit:anon"

    @mcp.custom_route("/cockpit/director/pending.json", methods=["GET"])
    async def director_pending(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        status = request.query_params.get("status") or "pending"
        try:
            limit = int(request.query_params.get("limit") or 100)
        except ValueError:
            limit = 100
        try:
            moves = await db.director_pending_moves(
                status=str(status), limit=min(limit, 500),
            )
            counts = await db.director_pending_counts()
        except Exception as e:
            logger.exception("director_pending failed")
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"moves": moves, "counts": counts})

    @mcp.custom_route("/cockpit/director/approve", methods=["POST"])
    async def director_approve(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        move_id = body.get("move_id")
        ids = body.get("move_ids")
        approved_by = _approver_id(request)
        try:
            if isinstance(ids, list) and ids:
                int_ids = [int(i) for i in ids]
                n = await db.director_bulk_approve(
                    move_ids=int_ids, approved_by=approved_by,
                )
                return JSONResponse({"approved": int(n)})
            if move_id is None:
                return JSONResponse(
                    {"error": "move_id or move_ids required"}, status_code=400
                )
            ok = await db.director_approve_move(
                move_id=int(move_id), approved_by=approved_by,
            )
            return JSONResponse({"approved": 1 if ok else 0})
        except Exception as e:
            logger.exception("director_approve failed")
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/cockpit/director/reject", methods=["POST"])
    async def director_reject(request: Request):
        if not cockpit_auth_ok(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        move_id = body.get("move_id")
        if move_id is None:
            return JSONResponse({"error": "move_id required"}, status_code=400)
        approved_by = _approver_id(request)
        try:
            ok = await db.director_reject_move(
                move_id=int(move_id), approved_by=approved_by,
            )
        except Exception as e:
            logger.exception("director_reject failed")
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"rejected": 1 if ok else 0})

    @mcp.custom_route("/cockpit/director", methods=["GET"])
    async def director_ui(request: Request):
        if not cockpit_auth_ok(request):
            return _unauth_html()
        token = request.query_params.get("token", "")
        return HTMLResponse(_render_director(token))


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
    /* mobile: <=640px */
    @media (max-width: 640px) {{
      body {{ font-size: 15px; }}
      .top {{
        flex-wrap: wrap; gap: 4px; padding: 8px 12px;
      }}
      .top h1 {{ font-size: 15px; }}
      .top .meta {{ font-size: 11px; flex-basis: 100%; }}
      .layout {{
        padding: 8px; gap: 8px;
      }}
      .col {{ gap: 8px; }}
      .panel h2 {{
        padding: 8px 12px; font-size: 12px;
        flex-wrap: wrap; gap: 4px;
      }}
      .panel .body {{ padding: 10px 12px; }}
      /* chat fills viewport on mobile */
      .col:first-child .panel {{
        min-height: 60vh;
      }}
      #chat-log {{ padding: 10px 12px; }}
      .msg {{ max-width: 92%; font-size: 14px; }}
      #chat-form {{
        flex-direction: column; gap: 6px; padding: 8px;
      }}
      #chat-input {{
        font-size: 16px; /* prevents iOS zoom-on-focus */
        min-height: 44px;
      }}
      #chat-form button {{ width: 100%; padding: 10px 14px; font-size: 14px; }}
      /* intent form: stack to single column */
      #intent-form {{ grid-template-columns: 1fr; padding: 8px 12px !important; }}
      #intent-form input, #intent-form textarea {{ font-size: 16px; padding: 8px 10px; }}
      #intent-form .row-full {{ flex-wrap: wrap; gap: 6px; }}
      #intent-form button {{ flex: 1; min-width: 120px; padding: 10px 12px; }}
      .quick-btns {{ padding: 6px 12px 10px; }}
      .quick-btns button {{ flex: 1 1 calc(50% - 6px); font-size: 12px; padding: 8px 10px; }}
      /* local bridge form */
      #local-form {{ padding: 8px 12px !important; }}
      #local-form select, #local-form textarea, #local-form input {{ font-size: 16px !important; }}
      #local-form button {{ width: 100%; padding: 10px 14px; font-size: 14px; }}
      /* tables: horizontal scroll instead of squish */
      .panel .body table {{ display: block; overflow-x: auto; white-space: nowrap; }}
      th, td {{ padding: 6px 8px; }}
    }}
    /* very small (≤380px): tighten further */
    @media (max-width: 380px) {{
      .top h1 {{ font-size: 14px; }}
      .layout {{ padding: 6px; }}
      .panel h2 {{ font-size: 11px; padding: 7px 10px; }}
      .quick-btns button {{ flex-basis: 100%; }}
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
    .msg.tool {{
      background: #0d1117; border: 1px dashed var(--border);
      align-self: stretch; max-width: 100%; padding: 6px 10px;
      font-size: 12px; color: var(--fg); white-space: normal;
    }}
    .msg.tool.tool-ok .tool-ico {{ color: var(--ok); font-weight: 700; }}
    .msg.tool.tool-err .tool-ico {{ color: var(--err); font-weight: 700; }}
    .msg.tool .tool-head code {{ background: #1f2937; padding: 1px 5px; border-radius: 3px; }}
    .msg.tool .tool-args {{ color: var(--muted); font-family: ui-monospace, monospace; font-size: 11px; }}
    .msg.tool .tool-summary {{ margin-top: 4px; color: var(--muted); }}
    .msg.tool .tool-summary b {{ color: var(--accent); }}
    .msg.tool details.tool-raw {{ margin-top: 4px; }}
    .msg.tool details.tool-raw summary {{ cursor: pointer; color: var(--muted); font-size: 11px; }}
    .msg.tool details.tool-raw pre {{
      background: #010409; border: 1px solid var(--border); border-radius: 4px;
      padding: 6px 8px; overflow-x: auto; font-size: 11px; color: var(--fg);
      max-height: 240px;
    }}
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
        <h2>director chat <span style="font-weight:400;font-size:11px">deepseek · v4-flash</span></h2>
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

function addToolCallMsg(tc) {{
  // tc = {{name, arguments, result, ok, hop}}
  const log = document.getElementById("chat-log");
  const wrap = document.createElement("div");
  wrap.className = "msg tool" + (tc.ok ? " tool-ok" : " tool-err");
  const head = document.createElement("div");
  head.className = "tool-head";
  const ico = tc.ok ? "✓" : "⚠";
  head.innerHTML = "<span class='tool-ico'>" + ico + "</span>" +
    " <code>" + escapeHtml(tc.name) + "</code> " +
    "<span class='tool-args'>" + escapeHtml(JSON.stringify(tc.arguments || {{}})) + "</span>";
  wrap.appendChild(head);
  // Highlight queued move IDs / request IDs so they're glanceable.
  const r = tc.result || {{}};
  const summary = document.createElement("div");
  summary.className = "tool-summary";
  if (r.queued_move_id) {{
    summary.innerHTML = "queued move <b>#" + r.queued_move_id + "</b>" +
      " (" + escapeHtml(r.kind || "?") + ", " + escapeHtml(r.status || "pending") + ")" +
      " — approve in queue panel";
  }} else if (r.request_id) {{
    summary.innerHTML = "capability request <b>#" + r.request_id + "</b>" +
      " (" + escapeHtml(r.status || "pending") + ")";
  }} else if (r.error) {{
    summary.innerHTML = "<span class='err'>" + escapeHtml(r.error) + "</span>";
  }} else if (typeof r.count === "number") {{
    summary.textContent = r.count + " rows";
  }} else {{
    summary.textContent = "ok";
  }}
  wrap.appendChild(summary);
  // Collapsible raw JSON for inspection.
  const det = document.createElement("details");
  det.className = "tool-raw";
  const sum = document.createElement("summary");
  sum.textContent = "raw";
  det.appendChild(sum);
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(r, null, 2);
  det.appendChild(pre);
  wrap.appendChild(det);
  log.appendChild(wrap);
  log.scrollTop = log.scrollHeight;
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
    // Render any tool calls inline first so the UI matches the order
    // of work — tool actions appear, then the assistant's recap.
    if (Array.isArray(d.tool_calls)) {{
      for (const tc of d.tool_calls) {{
        addToolCallMsg(tc);
      }}
      // If a propose_* tool fired, refresh the pending approvals panel
      // so the new row appears without a manual reload.
      const filed = d.tool_calls.some(t =>
        t.ok && t.result && t.result.queued_move_id);
      if (filed && typeof refreshDirectorPending === "function") {{
        refreshDirectorPending();
      }}
    }}
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


def _render_director(token: str) -> str:
    """Director approval queue UI — list pending moves, approve/reject, bulk."""
    safe_token = h(token)
    return f"""<!DOCTYPE html>
<html><head>
  <title>motto director · approvals</title>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {{
      --bg: #0e1116; --panel: #161b22; --border: #2d333b;
      --fg: #e6edf3; --muted: #7d8590; --accent: #2f81f7;
      --ok: #3fb950; --warn: #d29922; --err: #f85149;
      --kind-issue: #d29922; --kind-spawn: #2f81f7;
      --kind-merge: #3fb950; --kind-nudge: #a371f7;
      --kind-compound: #f78166;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg); color: var(--fg); font-size: 14px; min-height: 100vh;
    }}
    .top {{
      display: flex; align-items: center; justify-content: space-between;
      padding: 10px 16px; border-bottom: 1px solid var(--border); background: var(--panel);
      flex-wrap: wrap; gap: 8px;
    }}
    .top h1 {{ margin: 0; font-size: 16px; font-weight: 600; }}
    .top a {{ color: var(--accent); text-decoration: none; font-size: 12px; }}
    .top .meta {{ color: var(--muted); font-size: 12px; }}
    .toolbar {{
      display: flex; gap: 8px; padding: 10px 16px;
      border-bottom: 1px solid var(--border); background: var(--panel);
      flex-wrap: wrap; align-items: center;
    }}
    .toolbar select, .toolbar button {{
      background: var(--bg); color: var(--fg); border: 1px solid var(--border);
      padding: 6px 12px; border-radius: 6px; font-size: 13px; cursor: pointer;
    }}
    .toolbar button:hover {{ border-color: var(--accent); }}
    .toolbar .counts {{ color: var(--muted); font-size: 12px; margin-left: auto; }}
    .toolbar .counts span {{ margin-left: 10px; }}
    .toolbar .counts .pending {{ color: var(--warn); }}
    .toolbar .counts .approved {{ color: var(--accent); }}
    .toolbar .counts .applied {{ color: var(--ok); }}
    .toolbar .counts .rejected {{ color: var(--err); }}
    .list {{ padding: 12px; display: flex; flex-direction: column; gap: 10px; }}
    .move {{
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 8px; padding: 12px; display: flex; gap: 12px;
      align-items: flex-start;
    }}
    .move .check {{ flex-shrink: 0; margin-top: 4px; }}
    .move .body {{ flex: 1; min-width: 0; }}
    .move .head {{
      display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
      margin-bottom: 6px;
    }}
    .move .head .priority {{
      background: #21262d; color: var(--muted); padding: 2px 6px;
      border-radius: 4px; font-size: 11px; font-family: ui-monospace, monospace;
    }}
    .move .head .priority.p-high {{ background: #6e1c1c; color: #ffeded; }}
    .move .head .priority.p-med  {{ background: #5a3a09; color: #ffe5b4; }}
    .move .head .repo {{
      color: var(--muted); font-size: 12px; font-family: ui-monospace, monospace;
    }}
    .move .head .kind {{
      padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600;
      text-transform: uppercase; letter-spacing: 0.4px; color: #fff;
    }}
    .move .head .kind.file_issue {{ background: var(--kind-issue); }}
    .move .head .kind.spawn_session {{ background: var(--kind-spawn); }}
    .move .head .kind.merge_pr {{ background: var(--kind-merge); }}
    .move .head .kind.nudge_pipeline {{ background: var(--kind-nudge); }}
    .move .head .kind.compound_pr {{ background: var(--kind-compound); }}
    .move .head .kind.noop {{ background: var(--muted); }}
    .move .title {{
      font-weight: 600; font-size: 14px; margin: 0 0 4px;
      word-break: break-word;
    }}
    .move .rationale {{
      color: var(--muted); font-size: 13px; line-height: 1.45;
      word-break: break-word;
    }}
    .move .rationale.long {{
      max-height: 4.5em; overflow: hidden; position: relative;
    }}
    .move .rationale.expanded {{ max-height: none; }}
    .move .show-more {{
      color: var(--accent); font-size: 12px; cursor: pointer;
      background: none; border: none; padding: 0; margin-top: 4px;
    }}
    .move .footer {{
      margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap;
      align-items: center;
    }}
    .move .footer .meta {{
      color: var(--muted); font-size: 11px; margin-right: auto;
      font-family: ui-monospace, monospace;
    }}
    .move .footer button {{
      background: var(--bg); color: var(--fg); border: 1px solid var(--border);
      padding: 5px 12px; border-radius: 6px; font-size: 12px; cursor: pointer;
      font-weight: 500;
    }}
    .move .footer button.approve {{ border-color: var(--ok); color: var(--ok); }}
    .move .footer button.approve:hover {{ background: var(--ok); color: #fff; }}
    .move .footer button.reject {{ border-color: var(--err); color: var(--err); }}
    .move .footer button.reject:hover {{ background: var(--err); color: #fff; }}
    .move .footer button:disabled {{
      opacity: 0.5; cursor: not-allowed;
    }}
    .empty {{
      padding: 40px; text-align: center; color: var(--muted);
    }}
    .err-banner {{
      background: #3d1d1d; color: var(--err); padding: 10px 16px;
      border-bottom: 1px solid var(--err); font-size: 13px;
    }}
    /* mobile */
    @media (max-width: 640px) {{
      body {{ font-size: 15px; }}
      .top {{ padding: 8px 12px; }}
      .toolbar {{ padding: 8px 12px; gap: 6px; }}
      .toolbar .counts {{ flex-basis: 100%; margin-left: 0; }}
      .toolbar .counts span {{ margin-left: 0; margin-right: 10px; }}
      .toolbar select, .toolbar button {{ font-size: 13px; padding: 8px 12px; }}
      .list {{ padding: 8px; }}
      .move {{ padding: 10px; }}
      .move .footer {{ gap: 4px; }}
      .move .footer .meta {{ flex-basis: 100%; margin-right: 0; }}
      .move .footer button {{ flex: 1; padding: 8px; }}
    }}
  </style>
</head><body>
  <div class="top">
    <div>
      <h1>director · approvals</h1>
      <div class="meta">pending moves awaiting human review</div>
    </div>
    <div><a href="/cockpit?token={safe_token}">← back to cockpit</a></div>
  </div>
  <div class="toolbar">
    <select id="status-filter">
      <option value="pending" selected>pending</option>
      <option value="approved">approved</option>
      <option value="applied">applied</option>
      <option value="rejected">rejected</option>
      <option value="failed">failed</option>
      <option value="expired">expired</option>
    </select>
    <button id="refresh-btn">refresh</button>
    <button id="approve-all-btn">approve all visible</button>
    <span class="counts" id="counts"></span>
  </div>
  <div id="err-banner"></div>
  <div class="list" id="moves-list">
    <div class="empty">loading…</div>
  </div>
<script>
const Q = "?token=" + encodeURIComponent("{safe_token}");

function escapeHtml(s) {{
  return String(s == null ? "" : s)
    .replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;")
    .replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}}

function priorityClass(p) {{
  if (p >= 8) return "p-high";
  if (p >= 5) return "p-med";
  return "";
}}

function renderCounts(c) {{
  const el = document.getElementById("counts");
  if (!c) {{ el.textContent = ""; return; }}
  const parts = [];
  for (const k of ["pending","approved","applied","rejected","failed","expired"]) {{
    if (c[k]) parts.push('<span class="' + k + '">' + k + ': ' + c[k] + '</span>');
  }}
  el.innerHTML = parts.join("");
}}

function renderMove(m) {{
  const pCls = priorityClass(m.priority || 0);
  const isPending = m.status === "pending";
  const rationale = m.rationale || "";
  const long = rationale.length > 200;
  const meta = (m.created_at ? m.created_at.replace("T"," ").slice(0,16) + " · " : "")
             + "id " + m.id
             + (m.run_id ? " · run " + String(m.run_id).slice(0,8) : "");
  const approveDisabled = !isPending ? "disabled" : "";
  const rejectDisabled = !isPending ? "disabled" : "";
  return `
    <div class="move" data-id="${{m.id}}">
      ${{isPending ? '<input type="checkbox" class="check" data-id="' + m.id + '">' : ''}}
      <div class="body">
        <div class="head">
          <span class="priority ${{pCls}}">P${{m.priority || 0}}</span>
          <span class="kind ${{escapeHtml(m.kind)}}">${{escapeHtml(m.kind)}}</span>
          <span class="repo">${{escapeHtml(m.repo)}}</span>
        </div>
        <div class="title">${{escapeHtml(m.title)}}</div>
        <div class="rationale ${{long ? 'long' : ''}}">${{escapeHtml(rationale)}}</div>
        ${{long ? '<button class="show-more" data-id="' + m.id + '">show more</button>' : ''}}
        <div class="footer">
          <span class="meta">${{escapeHtml(meta)}}${{
            m.approved_by ? ' · by ' + escapeHtml(m.approved_by) : ''
          }}</span>
          <button class="approve" data-id="${{m.id}}" ${{approveDisabled}}>approve</button>
          <button class="reject" data-id="${{m.id}}" ${{rejectDisabled}}>reject</button>
        </div>
      </div>
    </div>`;
}}

async function refresh() {{
  const status = document.getElementById("status-filter").value;
  const list = document.getElementById("moves-list");
  const banner = document.getElementById("err-banner");
  banner.innerHTML = "";
  try {{
    const url = "/cockpit/director/pending.json" + Q
              + "&status=" + encodeURIComponent(status);
    const r = await fetch(url, {{cache: "no-store"}});
    const d = await r.json();
    if (d.error) {{
      banner.innerHTML = '<div class="err-banner">' + escapeHtml(d.error) + '</div>';
      list.innerHTML = '<div class="empty">error</div>';
      return;
    }}
    renderCounts(d.counts);
    if (!d.moves || !d.moves.length) {{
      list.innerHTML = '<div class="empty">no ' + escapeHtml(status) + ' moves</div>';
      return;
    }}
    list.innerHTML = d.moves.map(renderMove).join("");
  }} catch (err) {{
    banner.innerHTML = '<div class="err-banner">' + escapeHtml(err.message) + '</div>';
  }}
}}

async function approveOne(id) {{
  const r = await fetch("/cockpit/director/approve" + Q, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{move_id: parseInt(id, 10)}})
  }});
  const d = await r.json();
  if (d.error) alert(d.error);
  refresh();
}}

async function rejectOne(id) {{
  if (!confirm("Reject move #" + id + "?")) return;
  const r = await fetch("/cockpit/director/reject" + Q, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{move_id: parseInt(id, 10)}})
  }});
  const d = await r.json();
  if (d.error) alert(d.error);
  refresh();
}}

async function approveAllVisible() {{
  const ids = Array.from(document.querySelectorAll('.move .check'))
    .map(el => parseInt(el.dataset.id, 10))
    .filter(Number.isFinite);
  if (!ids.length) return;
  if (!confirm("Approve " + ids.length + " moves?")) return;
  const r = await fetch("/cockpit/director/approve" + Q, {{
    method: "POST",
    headers: {{"content-type": "application/json"}},
    body: JSON.stringify({{move_ids: ids}})
  }});
  const d = await r.json();
  if (d.error) alert(d.error); else alert("approved " + d.approved);
  refresh();
}}

document.addEventListener("click", (ev) => {{
  const t = ev.target;
  if (!t || !t.dataset || !t.dataset.id) return;
  if (t.classList.contains("approve")) approveOne(t.dataset.id);
  else if (t.classList.contains("reject")) rejectOne(t.dataset.id);
  else if (t.classList.contains("show-more")) {{
    const move = t.closest(".move");
    if (move) {{
      const ra = move.querySelector(".rationale");
      if (ra) {{
        ra.classList.toggle("expanded");
        const expanded = ra.classList.contains("expanded");
        t.textContent = expanded ? "show less" : "show more";
      }}
    }}
  }}
}});

document.getElementById("refresh-btn").addEventListener("click", refresh);
document.getElementById("approve-all-btn").addEventListener("click", approveAllVisible);
document.getElementById("status-filter").addEventListener("change", refresh);

refresh();
setInterval(refresh, 15000);
</script>
</body></html>"""
