"""DeepSeek LLM client — OpenAI-compatible chat completions API.

Provider: DeepSeek V4-Flash, single-provider, OpenAI-compatible,
1M context. Mirrors motto-director's deepseek-only chain. Earlier
Claude Max / Claude Code CLI shims are gone.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Director persona ──────────────────────────────────────────────────────────

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


# ── DeepSeek API config ───────────────────────────────────────────────────────

DEEPSEEK_BASE_URL = os.environ.get(
    "DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1"
)
DEEPSEEK_DEFAULT_MODEL = os.environ.get(
    "DEEPSEEK_MODEL", "deepseek-v4-flash"
)
DEEPSEEK_TIMEOUT_S = float(os.environ.get("DEEPSEEK_TIMEOUT_S", "120"))


async def call_deepseek(
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
    """Pull the text content out of a DeepSeek messages response."""
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
