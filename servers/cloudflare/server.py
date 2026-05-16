"""Cloudflare MCP server — wraps the Cloudflare API for the Motto fleet.

Exposes accounts, zones, DNS records, Workers, Pages, KV namespaces, and
R2 buckets. Read tools are unrestricted; every mutating tool requires
``confirm=True``.

Run with ``python -m servers.cloudflare`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.

Auth: ``CLOUDFLARE_API_TOKEN`` from env (canonical home is Doppler
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

CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
DEFAULT_TIMEOUT = 30.0

mcp = FastMCP(
    "motto-cloudflare",
    instructions=(
        "Cloudflare management for the Motto fleet — accounts, zones, DNS, "
        "Workers, Pages, KV, R2. Reads are unrestricted; every mutating tool "
        "(create_dns_record, delete_dns_record, purge_zone_cache, "
        "redeploy_pages) requires confirm=true. "
        "Token read from CLOUDFLARE_API_TOKEN env (Doppler motto-core/prd)."
    ),
)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class CloudflareClient:
    """Thin async wrapper around the Cloudflare REST API.

    Cloudflare wraps every response in ``{success, errors, messages, result}``.
    ``request`` unwraps to ``result`` on success and raises with the ``errors``
    array on failure.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or os.environ.get("CLOUDFLARE_API_TOKEN")
        if not self._token:
            raise RuntimeError(
                "CLOUDFLARE_API_TOKEN is required. Set it via Doppler "
                "(motto-core/prd) and inject at runtime."
            )
        self._client = httpx.AsyncClient(
            base_url=CLOUDFLARE_API,
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
        """Issue an HTTP request and return the unwrapped ``result`` field.

        Cloudflare's standard envelope is
        ``{"success": bool, "errors": [...], "messages": [...], "result": ...}``.
        On ``success: false`` (or any 4xx/5xx) we raise RuntimeError including
        the joined errors so callers see *why* it failed, not just status.
        """
        resp = await self._client.request(method, path, params=params, json=json_body)
        return _handle_response(resp, method=method, path=path)


def _handle_response(resp: httpx.Response, *, method: str, path: str) -> Any:
    if resp.status_code == 204 or not resp.content:
        if resp.status_code >= 400:
            _raise_http_error(resp, method=method, path=path)
        return {}
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}

    # Cloudflare-shaped envelope: prefer the success/errors signal over status
    # code (some endpoints return 200 + success:false on partial failure).
    if isinstance(data, dict) and "success" in data:
        if not data.get("success", False):
            _raise_cf_error(data, method=method, path=path, status=resp.status_code)
        return data.get("result")

    # Non-standard payload (e.g. raw error before reaching CF). Treat status
    # code as authoritative here.
    if resp.status_code >= 400:
        _raise_http_error(resp, method=method, path=path, parsed=data)
    return data


def _raise_cf_error(data: dict[str, Any], *, method: str, path: str, status: int) -> None:
    errors = data.get("errors") or []
    parts: list[str] = []
    for err in errors:
        if isinstance(err, dict):
            code = err.get("code")
            msg = err.get("message", "")
            parts.append(f"[{code}] {msg}" if code is not None else msg)
        else:
            parts.append(str(err))
    joined = "; ".join(p for p in parts if p) or "unknown error"
    raise RuntimeError(f"Cloudflare {method} {path} failed [{status}]: {joined}")


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
    raise RuntimeError(f"Cloudflare {method} {path} failed [{resp.status_code}]: {summary}")


# ---------------------------------------------------------------------------
# Lazy client holder — lets tests inject a fake without touching env
# ---------------------------------------------------------------------------


_client_holder: dict[str, CloudflareClient] = {}


def _client() -> CloudflareClient:
    if "c" not in _client_holder:
        _client_holder["c"] = CloudflareClient()
    return _client_holder["c"]


def set_client(client: CloudflareClient | None) -> None:
    """Swap the lazy client (tests). Pass None to reset."""
    if client is None:
        _client_holder.pop("c", None)
    else:
        _client_holder["c"] = client


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_accounts() -> list[dict[str, Any]]:
    """List Cloudflare accounts visible to the API token.

    Args: none.
    Returns: list of account dicts.
    """
    data = await _client().request("GET", "/accounts")
    return _as_list(data)


@mcp.tool()
async def list_zones(account_id: str | None = None) -> list[dict[str, Any]]:
    """List Cloudflare zones (domains). Optional account filter.

    Args:
        account_id: when set, only zones in this account are returned.
    Returns: list of zone dicts.
    """
    params = {"account.id": account_id} if account_id else None
    data = await _client().request("GET", "/zones", params=params)
    return _as_list(data)


@mcp.tool()
async def get_zone(zone_id: str) -> dict[str, Any]:
    """Fetch a zone by id.

    Args:
        zone_id: Cloudflare zone id.
    Returns: zone dict.
    """
    data = await _client().request("GET", f"/zones/{zone_id}")
    return _as_dict(data)


@mcp.tool()
async def list_dns_records(
    zone_id: str,
    type: str | None = None,
    name: str | None = None,
) -> list[dict[str, Any]]:
    """List DNS records in a zone, optionally filtered by record type or name.

    Args:
        zone_id: Cloudflare zone id.
        type: optional record type filter (A, AAAA, CNAME, TXT, MX, ...).
        name: optional record name filter.
    Returns: list of DNS record dicts.
    """
    params: dict[str, Any] = {}
    if type is not None:
        params["type"] = type
    if name is not None:
        params["name"] = name
    data = await _client().request(
        "GET",
        f"/zones/{zone_id}/dns_records",
        params=params or None,
    )
    return _as_list(data)


@mcp.tool()
async def list_workers(account_id: str) -> list[dict[str, Any]]:
    """List Workers scripts in an account.

    Args:
        account_id: Cloudflare account id.
    Returns: list of script dicts.
    """
    data = await _client().request("GET", f"/accounts/{account_id}/workers/scripts")
    return _as_list(data)


@mcp.tool()
async def get_worker(account_id: str, script_name: str) -> dict[str, Any]:
    """Fetch metadata for a single Workers script.

    Args:
        account_id: Cloudflare account id.
        script_name: script (Worker) name.
    Returns: script dict.
    """
    data = await _client().request("GET", f"/accounts/{account_id}/workers/scripts/{script_name}")
    return _as_dict(data)


@mcp.tool()
async def list_pages_projects(account_id: str) -> list[dict[str, Any]]:
    """List Cloudflare Pages projects in an account.

    Args:
        account_id: Cloudflare account id.
    Returns: list of Pages project dicts.
    """
    data = await _client().request("GET", f"/accounts/{account_id}/pages/projects")
    return _as_list(data)


@mcp.tool()
async def get_pages_project(account_id: str, project_name: str) -> dict[str, Any]:
    """Fetch a single Pages project.

    Args:
        account_id: Cloudflare account id.
        project_name: Pages project name.
    Returns: project dict.
    """
    data = await _client().request("GET", f"/accounts/{account_id}/pages/projects/{project_name}")
    return _as_dict(data)


@mcp.tool()
async def list_pages_deployments(account_id: str, project_name: str) -> list[dict[str, Any]]:
    """List recent deployments for a Pages project (newest first).

    Args:
        account_id: Cloudflare account id.
        project_name: Pages project name.
    Returns: list of deployment dicts.
    """
    data = await _client().request(
        "GET",
        f"/accounts/{account_id}/pages/projects/{project_name}/deployments",
    )
    return _as_list(data)


@mcp.tool()
async def list_kv_namespaces(account_id: str) -> list[dict[str, Any]]:
    """List Workers KV namespaces in an account.

    Args:
        account_id: Cloudflare account id.
    Returns: list of namespace dicts (id + title).
    """
    data = await _client().request("GET", f"/accounts/{account_id}/storage/kv/namespaces")
    return _as_list(data)


@mcp.tool()
async def list_r2_buckets(account_id: str) -> list[dict[str, Any]]:
    """List R2 buckets in an account.

    Args:
        account_id: Cloudflare account id.
    Returns: list of bucket dicts.
    """
    data = await _client().request("GET", f"/accounts/{account_id}/r2/buckets")
    # R2 wraps in {"buckets": [...]} inside `result`; normalize.
    if isinstance(data, dict) and "buckets" in data:
        buckets = data.get("buckets")
        return buckets if isinstance(buckets, list) else []
    return _as_list(data)


# ---------------------------------------------------------------------------
# Write tools (each gated by confirm=True)
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_dns_record(
    zone_id: str,
    type: str,
    name: str,
    content: str,
    ttl: int = 1,
    proxied: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a DNS record. confirm=True REQUIRED.

    Args:
        zone_id: Cloudflare zone id.
        type: record type (A, AAAA, CNAME, TXT, MX, ...).
        name: record name (e.g. "www" or "@" for root).
        content: record value (IP for A, target for CNAME, etc.).
        ttl: TTL in seconds; 1 means "automatic" in Cloudflare.
        proxied: whether to enable Cloudflare proxy/CDN for the record.
        confirm: must be True or the call is refused.
    Returns: the created DNS record dict.
    """
    if not confirm:
        _refuse(
            "create_dns_record",
            zone_id=zone_id,
            type=type,
            name=name,
        )
    data = await _client().request(
        "POST",
        f"/zones/{zone_id}/dns_records",
        json_body={
            "type": type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
        },
    )
    return _as_dict(data)


@mcp.tool()
async def delete_dns_record(
    zone_id: str,
    record_id: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Delete a DNS record. confirm=True REQUIRED.

    Args:
        zone_id: Cloudflare zone id.
        record_id: DNS record id (from list_dns_records).
        confirm: must be True or the call is refused.
    Returns: {"deleted": True, "zone_id", "record_id"}.
    """
    if not confirm:
        _refuse("delete_dns_record", zone_id=zone_id, record_id=record_id)
    await _client().request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
    return {"deleted": True, "zone_id": zone_id, "record_id": record_id}


@mcp.tool()
async def purge_zone_cache(zone_id: str, confirm: bool = False) -> dict[str, Any]:
    """Purge the entire Cloudflare cache for a zone. confirm=True REQUIRED.

    Args:
        zone_id: Cloudflare zone id.
        confirm: must be True or the call is refused.
    Returns: {"purged": True, "zone_id"}.
    """
    if not confirm:
        _refuse("purge_zone_cache", zone_id=zone_id)
    await _client().request(
        "POST",
        f"/zones/{zone_id}/purge_cache",
        json_body={"purge_everything": True},
    )
    return {"purged": True, "zone_id": zone_id}


@mcp.tool()
async def redeploy_pages(
    account_id: str,
    project_name: str,
    branch: str = "main",
    confirm: bool = False,
) -> dict[str, Any]:
    """Trigger a fresh deployment of a Pages project from the given branch.
    confirm=True REQUIRED.

    Args:
        account_id: Cloudflare account id.
        project_name: Pages project name.
        branch: source branch to deploy (default "main").
        confirm: must be True or the call is refused.
    Returns: the created deployment dict.
    """
    if not confirm:
        _refuse(
            "redeploy_pages",
            account_id=account_id,
            project_name=project_name,
            branch=branch,
        )
    data = await _client().request(
        "POST",
        f"/accounts/{account_id}/pages/projects/{project_name}/deployments",
        json_body={"branch": branch},
    )
    return _as_dict(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refuse(tool: str, **scope: Any) -> None:
    raise RuntimeError(
        f"{tool}: confirm=False; refusing to mutate. Re-call with confirm=True. scope={scope}"
    )


def _as_list(data: Any) -> list[dict[str, Any]]:
    """Cloudflare's `result` field for list endpoints is already a list. Be
    defensive against single-dict responses or empty bodies."""
    if isinstance(data, list):
        return data
    if data is None:
        return []
    if isinstance(data, dict):
        # Some endpoints (R2) wrap the list under a single key inside result.
        for v in data.values():
            if isinstance(v, list):
                return v
    return []


def _as_dict(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    return {}


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Cloudflare MCP server. stdio by default; set MCP_TRANSPORT=http
    to expose over HTTP (PORT, default 8083)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8083"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
