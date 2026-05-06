"""Noop verifier: trivially passes.

A noop move has no externally observable effect by definition. Verifying it
is mostly a smoke test of the verify_move plumbing itself — useful for
confirming the loop wires up before we trust real verifiers.
"""

from __future__ import annotations

from typing import Any

from .types import VerifyContext, VerifyResult


async def verify(move: dict[str, Any], ctx: VerifyContext) -> VerifyResult:
    return VerifyResult(
        status="passed",
        verifier="noop",
        evidence={
            "title": move.get("title"),
            "rationale": move.get("rationale"),
            "note": "noop verifier always passes; nothing observable to check.",
        },
        kpi_delta={},
    )
