"""Unit tests for the Doppler MCP server.

We mock DopplerClient so tests never touch the live API. The goal is to
verify the safety gates (confirm=true) and the shape of audit output.
"""

from __future__ import annotations

from typing import Any

import pytest

from servers.doppler.server import (
    CANONICAL_CONFIG,
    CANONICAL_PROJECT,
    DopplerClient,
    SecretDeleteInput,
    SecretRenameInput,
    SecretSetInput,
    build_server,
)


class FakeClient(DopplerClient):
    """In-memory stand-in for DopplerClient."""

    def __init__(self) -> None:  # type: ignore[override]
        # Skip parent __init__ — we don't want to require DOPPLER_TOKEN.
        self._token = "fake"
        self._audit_token = "fake-audit"
        self._client = None  # type: ignore[assignment]
        self.store: dict[tuple[str, str], dict[str, str]] = {
            (CANONICAL_PROJECT, CANONICAL_CONFIG): {
                "GITHUB_TOKEN": "ghp_canonical",
                "OPENAI_API_KEY": "sk-canonical",
                "CLAUDE_CODE_OAUTH_TOKEN": "claude-tok-1",
            },
            ("motto-sdr", "prd"): {
                "GITHUB_TOKEN": "ghp_canonical",  # duplicate, matches
                "APOLLO_API_KEY": "apollo-tok",
            },
            ("motto-conductor", "prd"): {
                "OPENAI_API_KEY": "sk-different",  # duplicate, drifts
            },
        }
        self.actions: list[dict[str, Any]] = []

    async def aclose(self) -> None:  # noqa: D401
        return

    async def list_projects(self) -> list[dict[str, Any]]:
        seen = sorted({p for (p, _) in self.store})
        return [{"slug": p, "name": p} for p in seen]

    async def list_configs(self, project: str) -> list[dict[str, Any]]:
        return [{"name": c} for (p, c) in self.store if p == project]

    async def list_secrets(
        self,
        project: str,
        config: str,
        *,
        include_values: bool = False,
        use_audit_token: bool = False,
    ) -> dict[str, dict[str, Any]]:
        raw = self.store.get((project, config), {})
        out: dict[str, dict[str, Any]] = {}
        for k, v in raw.items():
            row: dict[str, Any] = {"computed": "***"}
            if include_values:
                row["raw"] = v
            out[k] = row
        return out

    async def get_secret(self, project: str, config: str, name: str) -> dict[str, Any]:
        v = self.store.get((project, config), {}).get(name)
        if v is None:
            return {}
        return {"name": name, "raw": v, "computed": v}

    async def set_secret(
        self, project: str, config: str, name: str, value: str
    ) -> dict[str, Any]:
        self.store.setdefault((project, config), {})[name] = value
        self.actions.append({"action": "set", "project": project, "name": name})
        return {"ok": True}

    async def delete_secret(
        self, project: str, config: str, name: str
    ) -> dict[str, Any]:
        self.store.get((project, config), {}).pop(name, None)
        self.actions.append({"action": "delete", "project": project, "name": name})
        return {"ok": True}


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def server(client: FakeClient):
    return build_server(client=client)


async def _call(server, name: str, **kwargs) -> Any:
    """Invoke a registered FastMCP tool by name.

    FastMCP returns a ``ToolResult`` whose ``structured_content`` attr holds
    the JSON-serialized return value. For list-returning tools we unwrap the
    ``{"result": [...]}`` envelope FastMCP uses for non-dict outputs.
    """
    tool = await server.get_tool(name)
    result = await tool.run(arguments=kwargs)
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


@pytest.mark.asyncio
async def test_secrets_list_strips_values_by_default(server):
    out = await _call(
        server, "doppler_secrets_list", project=CANONICAL_PROJECT, config=CANONICAL_CONFIG
    )
    assert out["count"] == 3
    assert "GITHUB_TOKEN" in out["secrets"]
    assert "raw" not in out["secrets"]["GITHUB_TOKEN"]


@pytest.mark.asyncio
async def test_set_requires_confirm(server, client):
    out = await _call(
        server,
        "doppler_secret_set",
        payload={
            "name": "NEW_KEY",
            "value": "v",
        },
    )
    assert out["ok"] is False
    assert client.actions == []


@pytest.mark.asyncio
async def test_set_with_confirm_writes(server, client):
    out = await _call(
        server,
        "doppler_secret_set",
        payload=SecretSetInput(name="NEW_KEY", value="v", confirm=True).model_dump(),
    )
    assert out["ok"] is True
    assert (CANONICAL_PROJECT, CANONICAL_CONFIG) in client.store
    assert client.store[(CANONICAL_PROJECT, CANONICAL_CONFIG)]["NEW_KEY"] == "v"


@pytest.mark.asyncio
async def test_delete_requires_confirm(server, client):
    out = await _call(
        server,
        "doppler_secret_delete",
        payload=SecretDeleteInput(name="GITHUB_TOKEN").model_dump(),
    )
    assert out["ok"] is False
    # Still present
    assert "GITHUB_TOKEN" in client.store[(CANONICAL_PROJECT, CANONICAL_CONFIG)]


@pytest.mark.asyncio
async def test_rename_copies_then_deletes(server, client):
    out = await _call(
        server,
        "doppler_secret_rename",
        payload=SecretRenameInput(
            old_name="CLAUDE_CODE_OAUTH_TOKEN",
            new_name="CLAUDE_CODE_OAUTH_TOKEN_V2",
            confirm=True,
        ).model_dump(),
    )
    assert out["ok"] is True
    canon = client.store[(CANONICAL_PROJECT, CANONICAL_CONFIG)]
    assert "CLAUDE_CODE_OAUTH_TOKEN_V2" in canon
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in canon


@pytest.mark.asyncio
async def test_rename_keep_old(server, client):
    await _call(
        server,
        "doppler_secret_rename",
        payload=SecretRenameInput(
            old_name="GITHUB_TOKEN",
            new_name="GH_TOKEN_ALIAS",
            confirm=True,
            keep_old=True,
        ).model_dump(),
    )
    canon = client.store[(CANONICAL_PROJECT, CANONICAL_CONFIG)]
    assert "GH_TOKEN_ALIAS" in canon
    assert "GITHUB_TOKEN" in canon


@pytest.mark.asyncio
async def test_list_secret_names_returns_names_only(server):
    out = await _call(
        server, "list_secret_names", project=CANONICAL_PROJECT, config=CANONICAL_CONFIG
    )
    assert out["count"] == 3
    assert "GITHUB_TOKEN" in out["names"]
    # values must NOT appear anywhere in the response
    serialized = repr(out)
    assert "ghp_canonical" not in serialized
    assert "sk-canonical" not in serialized


async def _call_args(server, tool_name: str, args: dict[str, Any]) -> Any:
    tool = await server.get_tool(tool_name)
    result = await tool.run(arguments=args)
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


@pytest.mark.asyncio
async def test_read_secret_refuses_outside_allowlist(server, monkeypatch):
    # GITHUB_TOKEN is NOT in the default allowlist — must be refused.
    monkeypatch.delenv("MOTTO_DOPPLER_ALLOWLIST", raising=False)
    out = await _call_args(server, "read_secret", {"name": "GITHUB_TOKEN"})
    assert out["allowed"] is False
    assert out["status"] == 403
    assert "value" not in out


@pytest.mark.asyncio
async def test_read_secret_returns_value_when_allowed(server, client, monkeypatch):
    monkeypatch.delenv("MOTTO_DOPPLER_ALLOWLIST", raising=False)
    client.store[(CANONICAL_PROJECT, CANONICAL_CONFIG)]["OPENAI_API_KEY"] = "sk-canonical"
    out = await _call_args(server, "read_secret", {"name": "OPENAI_API_KEY"})
    assert out["allowed"] is True
    assert out["found"] is True
    assert out["value"] == "sk-canonical"


@pytest.mark.asyncio
async def test_read_secret_runtime_allowlist_override(server, monkeypatch):
    monkeypatch.setenv("MOTTO_DOPPLER_ALLOWLIST", "GITHUB_TOKEN")
    out = await _call_args(server, "read_secret", {"name": "GITHUB_TOKEN"})
    assert out["allowed"] is True
    assert out["value"] == "ghp_canonical"


@pytest.mark.asyncio
async def test_audit_finds_duplicates_and_drift(server):
    out = await _call(server, "doppler_audit_consolidation")
    dupes = {d["name"]: d for d in out["duplicates"]}

    # GITHUB_TOKEN appears in motto-core/prd and motto-sdr/prd with same value.
    assert "GITHUB_TOKEN" in dupes
    assert dupes["GITHUB_TOKEN"]["values_match"] is True

    # OPENAI_API_KEY appears in motto-core/prd and motto-conductor/prd with DIFFERENT values.
    assert "OPENAI_API_KEY" in dupes
    assert dupes["OPENAI_API_KEY"]["values_match"] is False

    # Hashes are short hex strings, never plaintext.
    for h in dupes["GITHUB_TOKEN"]["hashes"].values():
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)
        assert "ghp_canonical" not in h

    # APOLLO_API_KEY exists only in motto-sdr (non-canonical).
    non_canon_names = {d["name"] for d in out["non_canonical_only"]}
    assert "APOLLO_API_KEY" in non_canon_names
