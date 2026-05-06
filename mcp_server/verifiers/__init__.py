"""Verifier dispatch for the verify_move MCP tool.

Each verifier is a small async callable:

    async def verify(move: dict, ctx: VerifyContext) -> VerifyResult

`move` is the pending_moves row (already deserialized).
`ctx` exposes db handle, http client, and a `request_capability(name, why)`
helper that writes a fleet.capability_requests row and returns its id.

Verifiers should never raise — wrap their own work and return
VerifyResult(status="error", error="...") on internal failure.

Day 1 ships only `noop` (auto-pass) and `merge_pr` (gh CI check).
Per-repo verifiers (sdr, video, pipeline) are added reactively, in
response to capability_requests director files when it actually needs them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import noop as _noop
from . import merge_pr as _merge_pr


@dataclass
class VerifyResult:
    status: str  # passed | failed | inconclusive | error
    verifier: str
    evidence: dict[str, Any] = field(default_factory=dict)
    kpi_delta: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class VerifyContext:
    db: Any                               # mcp_server.db.Database
    http_get: Callable[..., Awaitable[Any]]
    http_post: Callable[..., Awaitable[Any]]
    request_capability: Callable[[str, str, str | None], Awaitable[int]]
    requested_by: str = "director"


# kind -> verifier callable
# Verifiers must accept (move: dict, ctx: VerifyContext) and return VerifyResult.
REGISTRY: dict[str, Callable[[dict[str, Any], VerifyContext], Awaitable[VerifyResult]]] = {
    "noop": _noop.verify,
    "merge_pr": _merge_pr.verify,
}


async def dispatch(move: dict[str, Any], ctx: VerifyContext) -> VerifyResult:
    kind = (move.get("kind") or "").strip()
    verifier = REGISTRY.get(kind)
    if verifier is None:
        return VerifyResult(
            status="inconclusive",
            verifier="missing",
            error=f"no verifier registered for kind={kind!r}",
            evidence={"kind": kind, "registered": sorted(REGISTRY.keys())},
        )
    try:
        return await verifier(move, ctx)
    except Exception as exc:  # noqa: BLE001
        return VerifyResult(
            status="error",
            verifier=verifier.__module__.rsplit(".", 1)[-1],
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
