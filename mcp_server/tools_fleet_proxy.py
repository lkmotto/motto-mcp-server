"""Fleet proxy tools — MCP wrappers for internal Northflank services.

Calls appraisal-pipeline and appraisal-cockpit via private Northflank DNS
(never leaves the project network). No public URL exposure needed.

Env vars consumed:
  PIPELINE_SECRET     — X-Pipeline-Secret header for appraisal-pipeline
  PIPELINE_BASE_URL   — override (default: http://appraisal-pipeline:8080)
  COCKPIT_BASE_URL    — override (default: http://appraisal-cockpit:3001)
"""

from __future__ import annotations

import os
from typing import Any

import httpx

# ── internal base URLs (Northflank private DNS) ──────────────────────────────
_PIPELINE_BASE = os.environ.get("PIPELINE_BASE_URL", "http://appraisal-pipeline:8080")
_COCKPIT_BASE = os.environ.get("COCKPIT_BASE_URL", "http://appraisal-cockpit:3001")
_PIPELINE_SECRET = os.environ.get("PIPELINE_SECRET", "")

_TIMEOUT = httpx.Timeout(60.0, connect=5.0)


def _pipeline_headers() -> dict[str, str]:
    h: dict[str, str] = {}
    if _PIPELINE_SECRET:
        h["X-Pipeline-Secret"] = _PIPELINE_SECRET
    return h


# ── appraisal-pipeline tools ─────────────────────────────────────────────────


async def pipeline_status() -> dict[str, Any]:
    """Return the JSON status of the last appraisal pipeline run."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_PIPELINE_BASE}/status", headers=_pipeline_headers())
        r.raise_for_status()
        return r.json()


async def pipeline_run(
    address: str,
    city: str,
    zip_code: str,
    county: str,
    state: str = "TX",
    apn: str | None = None,
    year_built: int | None = None,
    bedrooms: float | None = None,
    bathrooms: float | None = None,
    gla: int | None = None,
) -> dict[str, Any]:
    """Trigger the appraisal pipeline for a subject property.

    Required: address, city, zip_code, county.
    Returns immediately with a run_id — check pipeline_status() for progress.
    """
    body: dict[str, Any] = {
        "address": address,
        "city": city,
        "zip": zip_code,
        "county": county,
        "state": state,
    }
    if apn is not None:
        body["apn"] = apn
    if year_built is not None:
        body["year_built"] = year_built
    if bedrooms is not None:
        body["bedrooms"] = bedrooms
    if bathrooms is not None:
        body["bathrooms"] = bathrooms
    if gla is not None:
        body["gla"] = gla
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_PIPELINE_BASE}/run",
            json=body,
            headers=_pipeline_headers(),
        )
        r.raise_for_status()
        return r.json()


async def pipeline_submit_assignment(assignment_json: dict[str, Any]) -> dict[str, Any]:
    """Submit a full appraisal assignment dict to /assignments (cockpit alias of /run).

    Pass the raw assignment payload as a dict.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_PIPELINE_BASE}/assignments",
            json=assignment_json,
            headers=_pipeline_headers(),
        )
        r.raise_for_status()
        return r.json()


async def pipeline_email_watcher_status() -> dict[str, Any]:
    """Return the email watcher state: enabled, last_check, stats."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            f"{_PIPELINE_BASE}/email-watcher/status",
            headers=_pipeline_headers(),
        )
        r.raise_for_status()
        return r.json()


async def pipeline_email_watcher_run_latest() -> dict[str, Any]:
    """Scan inbox for the newest appraisal order email and trigger the pipeline."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_PIPELINE_BASE}/email-watcher/run-latest",
            headers=_pipeline_headers(),
        )
        r.raise_for_status()
        return r.json()


async def pipeline_sharpen() -> dict[str, Any]:
    """Trigger the pipeline self-sharpening cycle (prompt + comp-selection improvement)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(
            f"{_PIPELINE_BASE}/sharpen",
            headers=_pipeline_headers(),
        )
        r.raise_for_status()
        return r.json()


async def pipeline_sharpen_status() -> dict[str, Any]:
    """Return self-sharpening status and recent improvements."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(
            f"{_PIPELINE_BASE}/sharpen/status",
            headers=_pipeline_headers(),
        )
        r.raise_for_status()
        return r.json()


# ── appraisal-cockpit tools ───────────────────────────────────────────────────


async def cockpit_state() -> dict[str, Any]:
    """Return the live cockpit state JSON: queue, pending decisions, recent runs."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.get(f"{_COCKPIT_BASE}/api/state")
        r.raise_for_status()
        return r.json()


async def cockpit_submit_intent(
    intent: str, payload: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Post a manual intent / nudge to the cockpit (director signal).

    intent: short string like 'approve_comp', 'reject_value', 'escalate'.
    payload: optional context dict.
    """
    body = {"intent": intent, "payload": payload or {}}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(f"{_COCKPIT_BASE}/api/intent", json=body)
        r.raise_for_status()
        return r.json()


# ── registration helper ───────────────────────────────────────────────────────


def register_proxy_tools(mcp: Any) -> None:
    """Register all fleet proxy tools onto a FastMCP instance."""
    for fn in [
        pipeline_status,
        pipeline_run,
        pipeline_submit_assignment,
        pipeline_email_watcher_status,
        pipeline_email_watcher_run_latest,
        pipeline_sharpen,
        pipeline_sharpen_status,
        cockpit_state,
        cockpit_submit_intent,
    ]:
        mcp.tool(fn)
