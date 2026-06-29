"""Cockpit route registration — cockpit UI, chat, local tasks, director approvals."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .. import chat_tools
from ..auth import _unauth_html, cockpit_auth_ok
from ..db import Database
from ..fleet_context import _build_fleet_context
from ..handlers.deepseek import DIRECTOR_PERSONA, _extract_text, call_deepseek
from ..templates import _render_cockpit, _render_director

logger = logging.getLogger(__name__)


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
        events = await db.recent_events(since_minutes=60, agent_name=None, kind=None, limit=50)
        runs = await db.list_runs(agent_name=None, status=None, since_minutes=60 * 24, limit=10)
        return JSONResponse(
            {
                "now": datetime.now(UTC).isoformat(),
                "agents": agents,
                "recent_events": events,
                "recent_runs": runs,
            }
        )

    @mcp.custom_route("/cockpit/chat", methods=["POST"])
    async def cockpit_chat(request: Request):
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
        # cycle (server imports routes, not the other way around).
        from .. import server as _server

        verify_move_fn = _server.verify_move

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
                fn = tc.get("function") or {}
                name = fn.get("name") or ""
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = (
                        json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args or {})
                    )
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
                system=system + "\n\n[note: tool budget exhausted, summarize what you did]",
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
                "hops": (
                    (max(tc["hop"] for tc in tool_log if tc.get("hop") is not None) + 1)
                    if any(tc.get("hop") is not None for tc in tool_log)
                    else 0
                ),
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
            return JSONResponse({"error": "kind + payload (object) required"}, status_code=400)
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
            return JSONResponse({"error": "target_agent and kind required"}, status_code=400)
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
        token = request.query_params.get("token") or request.headers.get(
            "authorization", ""
        ).removeprefix("Bearer ")
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
                status=str(status),
                limit=min(limit, 500),
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
                    move_ids=int_ids,
                    approved_by=approved_by,
                )
                return JSONResponse({"approved": int(n)})
            if move_id is None:
                return JSONResponse({"error": "move_id or move_ids required"}, status_code=400)
            ok = await db.director_approve_move(
                move_id=int(move_id),
                approved_by=approved_by,
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
                move_id=int(move_id),
                approved_by=approved_by,
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
