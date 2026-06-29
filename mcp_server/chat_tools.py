"""Chat tool palette — DeepSeek function-calling bridge for /cockpit/chat.

This is the "Option A" wiring: director chat goes from read-only reporter
to write-intent executor. Every tool the LLM picks runs server-side here,
in `dispatch()`. Two classes of tool:

1. **Read tools** — query state (pending moves, verifications, capability
   requests, trust, fleet). Run inline, result injected back into the
   conversation as a `tool` role message. No approval gate needed.

2. **Write-intent tools** — `propose_*` filers. They never directly mutate
   external state. Each one writes a row into `public.pending_moves` with
   status='pending', so the existing cockpit approval queue (and the
   manual-mode gate `DIRECTOR_APPROVAL_MODE=manual`) is preserved. The
   chat session is the *intent surface*; the human is still the *trigger*.

   Capability requests are a special case: filing one is harmless (it's
   just a "please grant me X" record). The grant via
   `decide_capability_request` is the human gate, so chat can both file
   and decide — the latter is logged with `decided_by`.

The DeepSeek API supports OpenAI-style `tools=[...]` with `tool_choice='auto'`.
We expose JSON schemas matching that contract. The chat handler runs a
tool-call loop: while the model returns `finish_reason='tool_calls'`,
execute each call, append a `tool` message with the result, and re-prompt.
Cap at MAX_TOOL_HOPS to avoid runaway loops.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

# Hard cap on consecutive tool-call rounds in a single user turn. The model
# can chain reads + a propose in one turn (e.g. "list pending → propose
# verify_move on top one"), but we don't want runaway loops.
MAX_TOOL_HOPS = 6

# Director kinds the chat is allowed to propose. Anything destructive
# (merge_pr, spawn_session, compound_pr) deliberately stays out of the
# palette — the human files those via the director's normal cycle.
ALLOWED_PROPOSE_KINDS = {"verify_move", "file_issue", "noop"}


# ── Tool schemas (OpenAI / DeepSeek function-calling shape) ───────────────

TOOL_SCHEMAS: list[dict[str, Any]] = [
    # ── Reads ─────────────────────────────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "list_pending_moves",
            "description": (
                "List pending_moves rows from director's queue. Use this "
                "when the user asks 'what's queued', 'what needs approval', "
                "'show me the latest moves', or before proposing a "
                "verify_move on a recently-applied row."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "approved", "applied", "rejected"],
                        "description": "Filter by status. Default 'pending'.",
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_verifications",
            "description": (
                "List recent move_verifications rows. Use to see what "
                "verifiers have run and the outcomes (passed/failed/etc)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "move_id": {"type": "integer"},
                    "repo": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["passed", "failed", "inconclusive", "error"],
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_capability_requests",
            "description": (
                "List capability_requests — director (or chat) asking the "
                "human for resources (API keys, OAuth scopes, etc)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["pending", "granted", "denied"],
                    },
                    "limit": {"type": "integer", "default": 20},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_trust_scores",
            "description": (
                "Return rolling EWMA trust scores per scope. Use to answer "
                "'how trustworthy is the director on repo X' or 'what's "
                "global trust right now'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "Filter to one scope (e.g. 'global' or 'lkmotto/motto-director').",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fleet_status",
            "description": ("Snapshot of registered agents (last_seen, last_run, open intents)."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # ── Write intent (queues a pending_move; human approves) ──────────────
    {
        "type": "function",
        "function": {
            "name": "propose_verify_move",
            "description": (
                "Queue a verify_move on a previously-applied pending_move. "
                "This files a NEW pending_moves row of kind='verify_move' "
                "that, once approved by the human, runs the appropriate "
                "verifier and updates trust scores. Use when the user says "
                "'verify move N', 'check if N worked', or after listing "
                "applied moves and identifying one to grade."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_move_id": {
                        "type": "integer",
                        "description": "id of the move to verify",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Why this is worth verifying now.",
                    },
                    "priority": {"type": "integer", "default": 0},
                },
                "required": ["target_move_id", "rationale"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_file_issue",
            "description": (
                "Queue a file_issue move. Files an issue in the named repo "
                "once approved. Use sparingly — only when the user clearly "
                "wants a tracked task captured."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {
                        "type": "string",
                        "description": "owner/repo, e.g. 'lkmotto/motto-director'",
                    },
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "rationale": {"type": "string"},
                    "priority": {"type": "integer", "default": 0},
                },
                "required": ["repo", "title", "body", "rationale"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_noop",
            "description": (
                "Queue a noop move — useful as a sanity ping or for testing "
                "the approval queue. The verifier will auto-pass."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rationale": {"type": "string"},
                    "priority": {"type": "integer", "default": 0},
                },
                "required": ["rationale"],
            },
        },
    },
    # ── Capability requests + grants ──────────────────────────────────────
    {
        "type": "function",
        "function": {
            "name": "request_capability",
            "description": (
                "File a capability request — chat asks Luke to grant a "
                "resource (API key, connector, OAuth scope, etc). "
                "Idempotent: if an identical pending request exists, the "
                "existing one is returned."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Short stable name, e.g. 'github_token', 'pipedream:slack'.",
                    },
                    "justification": {
                        "type": "string",
                        "description": "Why this is needed and what unblocks.",
                    },
                    "repo": {"type": "string"},
                    "move_id": {"type": "integer"},
                },
                "required": ["capability", "justification"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decide_capability_request",
            "description": (
                "Grant or deny a pending capability request. Use only when "
                "Luke explicitly says 'grant request N' / 'deny request N'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "request_id": {"type": "integer"},
                    "decision": {
                        "type": "string",
                        "enum": ["granted", "denied"],
                    },
                    "grant_detail": {"type": "string"},
                    "deny_reason": {"type": "string"},
                },
                "required": ["request_id", "decision"],
            },
        },
    },
]


# ── Dispatcher ────────────────────────────────────────────────────────────


async def dispatch(
    *,
    name: str,
    arguments: dict[str, Any],
    db: Any,
    chat_user: str,
    verify_move_fn: Callable[..., Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run one tool call. Returns a JSON-serializable result dict.

    Errors are returned as `{"error": "..."}` rather than raised, so the
    LLM can see the failure and recover (or surface it to the user).
    """
    try:
        if name == "list_pending_moves":
            return await _list_pending_moves(db, arguments)
        if name == "list_verifications":
            return await _list_verifications(db, arguments)
        if name == "list_capability_requests":
            return await _list_capability_requests(db, arguments)
        if name == "get_trust_scores":
            return await _get_trust_scores(db, arguments)
        if name == "get_fleet_status":
            return {"agents": await db.fleet_status()}

        if name == "propose_verify_move":
            return await _propose_verify_move(db, arguments, chat_user)
        if name == "propose_file_issue":
            return await _propose_file_issue(db, arguments, chat_user)
        if name == "propose_noop":
            return await _propose_noop(db, arguments, chat_user)

        if name == "request_capability":
            return await _request_capability(db, arguments, chat_user)
        if name == "decide_capability_request":
            return await _decide_capability_request(db, arguments, chat_user)

        return {"error": f"unknown tool: {name}"}
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat_tools.dispatch failed name=%s", name)
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}


# ── Read tool impls ───────────────────────────────────────────────────────


async def _list_pending_moves(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    status = (args.get("status") or "pending").strip()
    limit = int(args.get("limit") or 20)
    rows = await db.director_pending_moves(status=status, limit=limit)
    # Trim payloads so the LLM gets a usable summary not a wall of JSON.
    out = []
    for r in rows:
        mp = r.get("move_payload") or {}
        if isinstance(mp, dict):
            mp_short = {k: mp[k] for k in list(mp)[:6]}
        else:
            mp_short = {}
        out.append(
            {
                "id": r.get("id"),
                "repo": r.get("repo"),
                "kind": r.get("kind"),
                "title": r.get("title"),
                "rationale": (r.get("rationale") or "")[:300],
                "priority": r.get("priority"),
                "status": r.get("status"),
                "created_at": r.get("created_at"),
                "applied_at": r.get("applied_at"),
                "move_payload": mp_short,
            }
        )
    return {"count": len(out), "moves": out}


async def _list_verifications(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    rows = await db.list_verifications(
        move_id=args.get("move_id"),
        repo=args.get("repo"),
        status=args.get("status"),
        limit=int(args.get("limit") or 20),
    )
    return {"count": len(rows), "verifications": rows}


async def _list_capability_requests(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    rows = await db.list_capability_requests(
        status=args.get("status"),
        limit=int(args.get("limit") or 20),
    )
    return {"count": len(rows), "requests": rows}


async def _get_trust_scores(db: Any, args: dict[str, Any]) -> dict[str, Any]:
    rows = await db.get_trust_scores(scope=args.get("scope"))
    return {"scores": rows}


# ── Write-intent tool impls ───────────────────────────────────────────────


def _chat_run_id(chat_user: str) -> str:
    return f"chat-{chat_user}-{int(time.time())}"


async def _propose_verify_move(db: Any, args: dict[str, Any], chat_user: str) -> dict[str, Any]:
    target = args.get("target_move_id")
    rationale = (args.get("rationale") or "").strip()
    if not target or not rationale:
        return {"error": "target_move_id and rationale are required"}

    target_move = await db.fetch_pending_move(int(target))
    if not target_move:
        return {"error": f"target move {target} not found"}

    payload = {
        "target_move_id": int(target),
        "target_kind": target_move.get("kind"),
        "target_repo": target_move.get("repo"),
    }
    row = await db.enqueue_pending_move(
        run_id=_chat_run_id(chat_user),
        repo=target_move.get("repo") or "lkmotto/motto-director",
        kind="verify_move",
        title=f"verify_move(target_id={target})",
        rationale=rationale,
        intent=f"chat-proposed-by:{chat_user}",
        priority=int(args.get("priority") or 0),
        move_payload=payload,
    )
    return {
        "ok": True,
        "queued_move_id": row.get("id"),
        "status": row.get("status"),
        "kind": row.get("kind"),
        "approval_required": True,
        "note": (
            f"Filed verify_move targeting move {target}. Approve in cockpit "
            "queue to run the verifier."
        ),
    }


async def _propose_file_issue(db: Any, args: dict[str, Any], chat_user: str) -> dict[str, Any]:
    repo = (args.get("repo") or "").strip()
    title = (args.get("title") or "").strip()
    body = (args.get("body") or "").strip()
    rationale = (args.get("rationale") or "").strip()
    if not (repo and title and body and rationale):
        return {"error": "repo, title, body, and rationale are required"}
    payload = {"repo": repo, "title": title, "body": body}
    row = await db.enqueue_pending_move(
        run_id=_chat_run_id(chat_user),
        repo=repo,
        kind="file_issue",
        title=title[:120],
        rationale=rationale,
        intent=f"chat-proposed-by:{chat_user}",
        priority=int(args.get("priority") or 0),
        move_payload=payload,
    )
    return {
        "ok": True,
        "queued_move_id": row.get("id"),
        "status": row.get("status"),
        "kind": row.get("kind"),
        "approval_required": True,
    }


async def _propose_noop(db: Any, args: dict[str, Any], chat_user: str) -> dict[str, Any]:
    rationale = (args.get("rationale") or "").strip()
    if not rationale:
        return {"error": "rationale is required"}
    row = await db.enqueue_pending_move(
        run_id=_chat_run_id(chat_user),
        repo="lkmotto/motto-director",
        kind="noop",
        title=f"chat noop: {rationale[:80]}",
        rationale=rationale,
        intent=f"chat-proposed-by:{chat_user}",
        priority=int(args.get("priority") or 0),
        move_payload={"source": "chat", "user": chat_user},
    )
    return {
        "ok": True,
        "queued_move_id": row.get("id"),
        "status": row.get("status"),
        "kind": row.get("kind"),
        "approval_required": True,
    }


async def _request_capability(db: Any, args: dict[str, Any], chat_user: str) -> dict[str, Any]:
    capability = (args.get("capability") or "").strip()
    justification = (args.get("justification") or "").strip()
    if not capability or not justification:
        return {"error": "capability and justification are required"}
    row = await db.file_capability_request(
        capability=capability,
        justification=justification,
        requested_by=f"chat:{chat_user}",
        repo=args.get("repo"),
        move_id=args.get("move_id"),
    )
    return {
        "ok": True,
        "request_id": row.get("id"),
        "status": row.get("status"),
        "note": "Capability request filed. Cockpit grants/denies it.",
    }


async def _decide_capability_request(
    db: Any, args: dict[str, Any], chat_user: str
) -> dict[str, Any]:
    request_id = args.get("request_id")
    decision = (args.get("decision") or "").strip()
    if not request_id or decision not in ("granted", "denied"):
        return {"error": "request_id + decision (granted|denied) required"}
    ok = await db.decide_capability_request(
        request_id=int(request_id),
        decision=decision,
        decided_by=f"chat:{chat_user}",
        grant_detail=args.get("grant_detail"),
        deny_reason=args.get("deny_reason"),
    )
    return {"ok": ok, "request_id": int(request_id), "decision": decision}
