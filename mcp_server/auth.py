"""Cockpit auth helpers — token/credential handling."""

from __future__ import annotations

import logging
import os

from starlette.requests import Request
from starlette.responses import HTMLResponse

logger = logging.getLogger(__name__)


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
