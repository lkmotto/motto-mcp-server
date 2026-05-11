"""Doppler MCP server — secret read/write/audit for the Motto fleet.

Run as a standalone process (``python -m servers.doppler``) or import
:func:`build_server` to mount it inside another FastMCP app.

The server reads credentials from the environment, never disk:

* ``DOPPLER_TOKEN``       — service token scoped to ``motto-core/prd`` (read+write)
* ``DOPPLER_AUDIT_TOKEN`` — optional personal token used by ``doppler_audit_consolidation``
                           when cross-project read is needed; falls back to
                           ``DOPPLER_TOKEN`` if missing.
* ``DOPPLER_WORKPLACE_ID``— optional, defaults to the Motto workplace.

Destructive tools (``doppler_secret_set``, ``doppler_secret_delete``,
``doppler_secret_rename``) require ``confirm=true``. Reads are unrestricted.

Audit output is structured: each row tags secrets that exist in multiple
projects, equal vs differing values (compared by SHA-256 hash, never by
plaintext), and stale duplicates.
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections import defaultdict
from typing import Any

import httpx
from fastmcp import FastMCP
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DOPPLER_API = "https://api.doppler.com/v3"
CANONICAL_PROJECT = "motto-core"
CANONICAL_CONFIG = "prd"
DEFAULT_WORKPLACE_ID = "0b22d1310a7c01d97530"


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class DopplerClient:
    """Thin async wrapper around the Doppler REST API."""

    def __init__(self, token: str | None = None, audit_token: str | None = None):
        self._token = token or os.environ.get("DOPPLER_TOKEN")
        self._audit_token = audit_token or os.environ.get("DOPPLER_AUDIT_TOKEN") or self._token
        if not self._token:
            raise RuntimeError(
                "DOPPLER_TOKEN is required. Set it via Doppler "
                f"({CANONICAL_PROJECT}/{CANONICAL_CONFIG}) and inject at runtime."
            )
        self._client = httpx.AsyncClient(
            base_url=DOPPLER_API,
            timeout=30.0,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        use_audit_token: bool = False,
    ) -> dict[str, Any]:
        token = self._audit_token if use_audit_token else self._token
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        resp = await self._client.request(
            method, path, params=params, json=json_body, headers=headers
        )
        if resp.status_code == 204 or not resp.content:
            return {}
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Doppler {method} {path} failed [{resp.status_code}]: {data}"
            )
        return data

    # ---- projects / configs ------------------------------------------------

    async def list_projects(self) -> list[dict[str, Any]]:
        data = await self._request(
            "GET", "/projects", params={"per_page": 100}, use_audit_token=True
        )
        return data.get("projects", [])

    async def list_configs(self, project: str) -> list[dict[str, Any]]:
        data = await self._request(
            "GET",
            "/configs",
            params={"project": project, "per_page": 100},
            use_audit_token=True,
        )
        return data.get("configs", [])

    # ---- secrets -----------------------------------------------------------

    async def list_secrets(
        self,
        project: str,
        config: str,
        *,
        include_values: bool = False,
        use_audit_token: bool = False,
    ) -> dict[str, dict[str, Any]]:
        data = await self._request(
            "GET",
            "/configs/config/secrets",
            params={
                "project": project,
                "config": config,
                "include_dynamic_secrets": "false",
            },
            use_audit_token=use_audit_token,
        )
        secrets = data.get("secrets", {}) or {}
        if not include_values:
            for v in secrets.values():
                if isinstance(v, dict):
                    v.pop("raw", None)
                    v.pop("computed", None)
        return secrets

    async def get_secret(
        self, project: str, config: str, name: str
    ) -> dict[str, Any]:
        data = await self._request(
            "GET",
            "/configs/config/secret",
            params={"project": project, "config": config, "name": name},
        )
        return data.get("secret", {})

    async def set_secret(
        self, project: str, config: str, name: str, value: str
    ) -> dict[str, Any]:
        data = await self._request(
            "POST",
            "/configs/config/secrets",
            json_body={
                "project": project,
                "config": config,
                "secrets": {name: value},
            },
        )
        return data

    async def delete_secret(
        self, project: str, config: str, name: str
    ) -> dict[str, Any]:
        # Doppler deletes by setting null
        data = await self._request(
            "POST",
            "/configs/config/secrets",
            json_body={
                "project": project,
                "config": config,
                "secrets": {name: None},
            },
        )
        return data


# ---------------------------------------------------------------------------
# Pydantic schemas (for tool inputs)
# ---------------------------------------------------------------------------


class _ProjectScope(BaseModel):
    project: str = Field(default=CANONICAL_PROJECT, description="Doppler project slug")
    config: str = Field(default=CANONICAL_CONFIG, description="Doppler config name")


class SecretSetInput(_ProjectScope):
    name: str = Field(..., description="Secret name (uppercase, snake_case)")
    value: str = Field(..., description="Plaintext value to write")
    confirm: bool = Field(
        default=False,
        description="Must be True to actually write — guards against accidental sets",
    )


class SecretDeleteInput(_ProjectScope):
    name: str
    confirm: bool = Field(default=False)


class SecretRenameInput(_ProjectScope):
    old_name: str
    new_name: str
    confirm: bool = Field(default=False)
    keep_old: bool = Field(
        default=False,
        description="If True, leave the old key in place as an alias (do not delete).",
    )


# ---------------------------------------------------------------------------
# Server factory
# ---------------------------------------------------------------------------


def build_server(client: DopplerClient | None = None) -> FastMCP:  # noqa: C901
    """Construct the FastMCP server with all Doppler tools registered."""

    server = FastMCP(
        "motto-doppler",
        instructions=(
            "Doppler secrets management for the Motto fleet. Canonical project is "
            f"'{CANONICAL_PROJECT}/{CANONICAL_CONFIG}'. Reads are unrestricted; writes "
            "and deletes require confirm=true. Use doppler_audit_consolidation to find "
            "duplicate or stale secrets across projects."
        ),
    )

    # Lazy-instantiate the client so import-time doesn't crash if the token
    # isn't yet in env (e.g. during pytest collection).
    _client_holder: dict[str, DopplerClient] = {}

    def _client() -> DopplerClient:
        if "c" not in _client_holder:
            _client_holder["c"] = client or DopplerClient()
        return _client_holder["c"]

    # ---- read tools --------------------------------------------------------

    @server.tool()
    async def doppler_projects_list() -> list[dict[str, Any]]:
        """List every Doppler project visible to the audit token."""
        return await _client().list_projects()

    @server.tool()
    async def doppler_configs_list(project: str = CANONICAL_PROJECT) -> list[dict[str, Any]]:
        """List configs (envs) for a project."""
        return await _client().list_configs(project)

    @server.tool()
    async def doppler_secrets_list(
        project: str = CANONICAL_PROJECT,
        config: str = CANONICAL_CONFIG,
        include_values: bool = False,
    ) -> dict[str, Any]:
        """List secret keys for a project/config. Set include_values=True to also
        return raw values (avoid this in chat — values can leak into transcripts).
        """
        secrets = await _client().list_secrets(
            project, config, include_values=include_values
        )
        return {
            "project": project,
            "config": config,
            "count": len(secrets),
            "secrets": secrets,
        }

    @server.tool()
    async def doppler_secret_get(
        name: str,
        project: str = CANONICAL_PROJECT,
        config: str = CANONICAL_CONFIG,
    ) -> dict[str, Any]:
        """Read a single secret. Returns the full Doppler row (raw + computed)."""
        return await _client().get_secret(project, config, name)

    # ---- write tools (gated) ----------------------------------------------

    @server.tool()
    async def doppler_secret_set(payload: SecretSetInput) -> dict[str, Any]:
        """Create or update a secret. confirm=true REQUIRED."""
        if not payload.confirm:
            return {
                "ok": False,
                "reason": "confirm=false; refusing to write",
                "would_write_to": f"{payload.project}/{payload.config}",
                "secret_name": payload.name,
            }
        await _client().set_secret(
            payload.project, payload.config, payload.name, payload.value
        )
        return {
            "ok": True,
            "project": payload.project,
            "config": payload.config,
            "name": payload.name,
            "action": "set",
        }

    @server.tool()
    async def doppler_secret_delete(payload: SecretDeleteInput) -> dict[str, Any]:
        """Delete a secret. confirm=true REQUIRED."""
        if not payload.confirm:
            return {
                "ok": False,
                "reason": "confirm=false; refusing to delete",
                "would_delete_from": f"{payload.project}/{payload.config}",
                "secret_name": payload.name,
            }
        await _client().delete_secret(payload.project, payload.config, payload.name)
        return {
            "ok": True,
            "project": payload.project,
            "config": payload.config,
            "name": payload.name,
            "action": "delete",
        }

    @server.tool()
    async def doppler_secret_rename(payload: SecretRenameInput) -> dict[str, Any]:
        """Rename a secret (copy old→new, optionally delete old). confirm=true REQUIRED.

        Use this for canonicalization fixes such as ``NORHTFLANK_API`` →
        ``NORTHFLANK_API_TOKEN`` or ``CLAUDE_OAUTH_TOKEN`` →
        ``CLAUDE_CODE_OAUTH_TOKEN``.
        """
        if not payload.confirm:
            return {
                "ok": False,
                "reason": "confirm=false; refusing to rename",
                "from": payload.old_name,
                "to": payload.new_name,
            }
        c = _client()
        old = await c.get_secret(payload.project, payload.config, payload.old_name)
        raw = old.get("raw") if isinstance(old, dict) else None
        if raw is None:
            return {
                "ok": False,
                "reason": "old secret not found or has no raw value",
                "from": payload.old_name,
            }
        await c.set_secret(payload.project, payload.config, payload.new_name, raw)
        if not payload.keep_old:
            await c.delete_secret(payload.project, payload.config, payload.old_name)
        return {
            "ok": True,
            "project": payload.project,
            "config": payload.config,
            "from": payload.old_name,
            "to": payload.new_name,
            "old_kept": payload.keep_old,
        }

    # ---- audit -------------------------------------------------------------

    @server.tool()
    async def doppler_audit_consolidation(
        configs_to_audit: list[str] | None = None,
    ) -> dict[str, Any]:
        """Cross-project key dedupe report.

        For every project that has the requested config(s) (default: ``prd``),
        list each secret key, the projects it appears in, and a SHA-256 hash
        of each value (NEVER the plaintext) so callers can spot duplicates and
        drift without leaking secrets through the MCP transport.

        Use this to drive the secret-consolidation work toward
        ``motto-core/prd`` as the single source of truth.
        """
        configs = configs_to_audit or [CANONICAL_CONFIG]
        c = _client()
        projects = await c.list_projects()

        # name -> { project: { config, hash, modified_at } }
        index: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        scanned: list[dict[str, str]] = []

        for proj in projects:
            slug = proj.get("slug") or proj.get("id") or proj.get("name")
            if not slug:
                continue
            for cfg_name in configs:
                try:
                    secrets = await c.list_secrets(
                        slug, cfg_name, include_values=True, use_audit_token=True
                    )
                except RuntimeError as e:
                    logger.info("audit skip %s/%s: %s", slug, cfg_name, e)
                    continue
                scanned.append({"project": slug, "config": cfg_name})
                for name, row in secrets.items():
                    raw = (row or {}).get("raw") or ""
                    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
                    index[name][slug] = {
                        "config": cfg_name,
                        "value_hash": digest,
                        "value_len": len(raw),
                    }

        # Build the report
        duplicates: list[dict[str, Any]] = []
        canonical_only: list[str] = []
        non_canonical_only: list[dict[str, Any]] = []

        for name, by_project in index.items():
            projects_with = sorted(by_project.keys())
            if len(projects_with) > 1:
                hashes = {meta["value_hash"] for meta in by_project.values()}
                duplicates.append(
                    {
                        "name": name,
                        "projects": projects_with,
                        "values_match": len(hashes) == 1,
                        "hashes": {p: m["value_hash"] for p, m in by_project.items()},
                    }
                )
            elif projects_with == [CANONICAL_PROJECT]:
                canonical_only.append(name)
            else:
                non_canonical_only.append(
                    {"name": name, "project": projects_with[0]}
                )

        return {
            "canonical_project": CANONICAL_PROJECT,
            "scanned": scanned,
            "summary": {
                "secrets_in_canonical_only": len(canonical_only),
                "secrets_with_duplicates": len(duplicates),
                "secrets_outside_canonical": len(non_canonical_only),
            },
            "duplicates": sorted(duplicates, key=lambda d: d["name"]),
            "non_canonical_only": sorted(
                non_canonical_only, key=lambda d: (d["project"], d["name"])
            ),
            "canonical_only_sample": sorted(canonical_only)[:25],
        }

    return server


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Doppler MCP server over stdio (for Claude Code) or HTTP."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    server = build_server()
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8081"))
        server.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
