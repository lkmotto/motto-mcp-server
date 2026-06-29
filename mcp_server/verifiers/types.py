"""Shared types for the verifier framework.

Extracted into a standalone module to avoid circular imports between
the package __init__ (which builds the REGISTRY by importing each
verifier) and individual verifier modules (which need VerifyContext /
VerifyResult to type-annotate their `verify` callables).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


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
