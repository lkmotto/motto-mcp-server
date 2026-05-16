"""Northflank MCP server — wraps the Northflank REST API for the Motto fleet.

Exposes Northflank projects, services, jobs (cron), secret groups, and the
small set of write actions Director needs to nudge deployments. Read tools
are unrestricted; every mutating tool requires ``confirm=True``.

Run with ``python -m servers.northflank`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.

Auth: ``NORTHFLANK_API_TOKEN`` from env (canonical home is Doppler
``motto-core/prd``). The token is read lazily so import-time / pytest
collection works without it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastmcp.server import FastMCP

logger = logging.getLogger(__name__)

NORTHFLANK_API = "https://api.northflank.com/v1"
DEFAULT_TIMEOUT = 30.0

mcp = FastMCP(
    "motto-northflank",
    instructions=(
        "Northflank deployment management for the Motto fleet. Reads are "
        "unrestricted; every mutating tool (restart_service, redeploy_service, "
        "trigger_job_run, resync_secret_group) requires confirm=true. "
        "Token read from NORTHFLANK_API_TOKEN env (Doppler motto-core/prd)."
    ),
)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class NorthflankClient:
    """Thin async wrapper around the Northflank REST API."""

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("NORTHFLANK_API_TOKEN")
        if not self._token:
            raise RuntimeError(
                "NORTHFLANK_API_TOKEN is required. Set it via Doppler "
                "(motto-core/prd) and inject at runtime."
            )
        self._client = httpx.AsyncClient(
            base_url=NORTHFLANK_API,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        """Issue an HTTP request and parse the body. 4xx/5xx raises RuntimeError
        with the path, status, and a summary of the response body."""
        resp = await self._client.request(method, path, params=params, json=json_body)
        return _handle_response(resp, method=method, path=path)

    async def request_text(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Like ``request`` but returns the response body as plain text. For
        endpoints (logs) that emit newline-delimited records rather than JSON."""
        resp = await self._client.request(method, path, params=params)
        if resp.status_code >= 400:
            _raise_http_error(resp, method=method, path=path)
        return resp.text


def _handle_response(resp: httpx.Response, *, method: str, path: str) -> Any:
    """Parse a Northflank JSON response. 4xx/5xx → RuntimeError with details."""
    if resp.status_code == 204 or not resp.content:
        if resp.status_code >= 400:
            _raise_http_error(resp, method=method, path=path)
        return {}
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        _raise_http_error(resp, method=method, path=path, parsed=data)
    # Northflank wraps successful payloads in {"data": ...}; unwrap when present
    # but fall back to the raw body so we don't drop fields.
    if isinstance(data, dict) and set(data.keys()) == {"data"}:
        return data["data"]
    return data


def _raise_http_error(
    resp: httpx.Response,
    *,
    method: str,
    path: str,
    parsed: Any = None,
) -> None:
    if parsed is None:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text
    summary = str(parsed)
    if len(summary) > 400:
        summary = summary[:397] + "..."
    raise RuntimeError(f"Northflank {method} {path} failed [{resp.status_code}]: {summary}")


# ---------------------------------------------------------------------------
# Lazy client holder — lets tests inject a fake without touching env
# ---------------------------------------------------------------------------


_client_holder: dict[str, NorthflankClient] = {}


def _client() -> NorthflankClient:
    if "c" not in _client_holder:
        _client_holder["c"] = NorthflankClient()
    return _client_holder["c"]


def set_client(client: NorthflankClient | None) -> None:
    """Swap the lazy client (tests). Pass None to reset."""
    if client is None:
        _client_holder.pop("c", None)
    else:
        _client_holder["c"] = client


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_projects() -> list[dict[str, Any]]:
    """List all Northflank projects visible to the API token.

    Args: none.
    Returns: list of project dicts as returned by Northflank.
    """
    data = await _client().request("GET", "/projects")
    return _as_list(data, key="projects")


@mcp.tool()
async def get_project(project_id: str) -> dict[str, Any]:
    """Fetch a single Northflank project by id.

    Args:
        project_id: Northflank project id (slug).
    Returns: project dict.
    """
    data = await _client().request("GET", f"/projects/{project_id}")
    return _as_dict(data, key="project")


@mcp.tool()
async def list_services(project_id: str) -> list[dict[str, Any]]:
    """List services in a project.

    Args:
        project_id: Northflank project id.
    Returns: list of service dicts.
    """
    data = await _client().request("GET", f"/projects/{project_id}/services")
    return _as_list(data, key="services")


@mcp.tool()
async def get_service(project_id: str, service_id: str) -> dict[str, Any]:
    """Fetch a single service.

    Args:
        project_id: Northflank project id.
        service_id: Northflank service id.
    Returns: service dict.
    """
    data = await _client().request("GET", f"/projects/{project_id}/services/{service_id}")
    return _as_dict(data, key="service")


@mcp.tool()
async def list_jobs(project_id: str) -> list[dict[str, Any]]:
    """List cron jobs in a project (Northflank "jobs").

    Args:
        project_id: Northflank project id.
    Returns: list of job dicts.
    """
    data = await _client().request("GET", f"/projects/{project_id}/jobs")
    return _as_list(data, key="jobs")


@mcp.tool()
async def get_job(project_id: str, job_id: str) -> dict[str, Any]:
    """Fetch a single cron job.

    Args:
        project_id: Northflank project id.
        job_id: Northflank job id.
    Returns: job dict.
    """
    data = await _client().request("GET", f"/projects/{project_id}/jobs/{job_id}")
    return _as_dict(data, key="job")


@mcp.tool()
async def list_secret_groups(project_id: str) -> list[dict[str, Any]]:
    """List secret groups in a project.

    Args:
        project_id: Northflank project id.
    Returns: list of secret-group dicts (metadata only; no secret values).
    """
    data = await _client().request("GET", f"/projects/{project_id}/secret-groups")
    return _as_list(data, key="secretGroups")


@mcp.tool()
async def get_secret_group(project_id: str, group_id: str) -> dict[str, Any]:
    """Fetch a single secret group's metadata.

    Args:
        project_id: Northflank project id.
        group_id: Secret group id.
    Returns: secret-group dict (metadata only).
    """
    data = await _client().request("GET", f"/projects/{project_id}/secret-groups/{group_id}")
    return _as_dict(data, key="secretGroup")


@mcp.tool()
async def get_recent_logs(
    project_id: str,
    service_id: str,
    lines: int = 200,
) -> dict[str, Any]:
    """Fetch the last N lines of a service's logs as plain text.

    Args:
        project_id: Northflank project id.
        service_id: Northflank service id.
        lines: max number of trailing lines to return (default 200).
    Returns: {"project_id", "service_id", "lines", "logs": "<text>"}.
    """
    text = await _client().request_text(
        "GET",
        f"/projects/{project_id}/services/{service_id}/logs",
        params={"lines": lines},
    )
    trimmed = "\n".join(text.splitlines()[-lines:])
    return {
        "project_id": project_id,
        "service_id": service_id,
        "lines": min(lines, len(trimmed.splitlines())),
        "logs": trimmed,
    }


# ---------------------------------------------------------------------------
# Write tools (each gated by confirm=True)
# ---------------------------------------------------------------------------


@mcp.tool()
async def restart_service(
    project_id: str,
    service_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Restart a service. confirm=True REQUIRED.

    Args:
        project_id: Northflank project id.
        service_id: Northflank service id.
        confirm: must be True or the call is refused.
    Returns: {"restarted": bool, "project_id", "service_id"}.
    """
    if not confirm:
        _refuse("restart_service", project_id=project_id, service_id=service_id)
    await _client().request(
        "POST",
        f"/projects/{project_id}/services/{service_id}/actions/restart",
    )
    return {
        "restarted": True,
        "project_id": project_id,
        "service_id": service_id,
    }


@mcp.tool()
async def redeploy_service(
    project_id: str,
    service_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Redeploy a service (rebuild + redeploy). confirm=True REQUIRED.

    Args:
        project_id: Northflank project id.
        service_id: Northflank service id.
        confirm: must be True or the call is refused.
    Returns: {"redeployed": bool, "project_id", "service_id"}.
    """
    if not confirm:
        _refuse("redeploy_service", project_id=project_id, service_id=service_id)
    await _client().request(
        "POST",
        f"/projects/{project_id}/services/{service_id}/actions/redeploy",
    )
    return {
        "redeployed": True,
        "project_id": project_id,
        "service_id": service_id,
    }


@mcp.tool()
async def trigger_job_run(
    project_id: str,
    job_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Trigger an ad-hoc run of a cron job. confirm=True REQUIRED.

    Args:
        project_id: Northflank project id.
        job_id: Northflank job id.
        confirm: must be True or the call is refused.
    Returns: {"triggered": bool, "project_id", "job_id"}.
    """
    if not confirm:
        _refuse("trigger_job_run", project_id=project_id, job_id=job_id)
    await _client().request(
        "POST",
        f"/projects/{project_id}/jobs/{job_id}/actions/run",
    )
    return {
        "triggered": True,
        "project_id": project_id,
        "job_id": job_id,
    }


@mcp.tool()
async def resync_secret_group(
    project_id: str,
    group_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Force a secret group to resync (e.g. after upstream Doppler change).
    confirm=True REQUIRED.

    Args:
        project_id: Northflank project id.
        group_id: Secret group id.
        confirm: must be True or the call is refused.
    Returns: {"resynced": bool, "project_id", "group_id"}.
    """
    if not confirm:
        _refuse("resync_secret_group", project_id=project_id, group_id=group_id)
    await _client().request(
        "POST",
        f"/projects/{project_id}/secret-groups/{group_id}/actions/resync",
    )
    return {
        "resynced": True,
        "project_id": project_id,
        "group_id": group_id,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refuse(tool: str, **scope: str) -> None:
    raise RuntimeError(
        f"{tool}: confirm=False; refusing to mutate. Re-call with confirm=True. scope={scope}"
    )


def _as_list(data: Any, *, key: str) -> list[dict[str, Any]]:
    """Northflank usually returns ``{"<key>": [...]}`` after the ``data`` unwrap.
    Accept either the wrapped shape or a bare list defensively."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        items = data.get(key)
        if isinstance(items, list):
            return items
    return []


def _as_dict(data: Any, *, key: str) -> dict[str, Any]:
    if isinstance(data, dict):
        if key in data and isinstance(data[key], dict):
            return data[key]
        return data
    return {}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Northflank MCP server. stdio by default; set MCP_TRANSPORT=http
    to expose over HTTP (PORT, default 8082)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8082"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
