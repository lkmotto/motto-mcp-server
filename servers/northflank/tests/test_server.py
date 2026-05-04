"""Unit tests for the Northflank MCP server.

We mock NorthflankClient so tests never touch the live API. The goal is to
verify the read parsing, the confirm-true safety gates, and the auth-error
contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from servers.northflank import server as nf
from servers.northflank.server import NorthflankClient, mcp, set_client


class FakeClient(NorthflankClient):
    """In-memory stand-in for NorthflankClient."""

    def __init__(self) -> None:  # type: ignore[override]
        # Skip parent __init__ — we don't want to require NORTHFLANK_API_TOKEN.
        self._token = "fake"
        self._client = None  # type: ignore[assignment]
        self.calls: list[dict[str, Any]] = []
        # path -> response (data unwrapped, as request() returns)
        self.responses: dict[tuple[str, str], Any] = {}
        self.text_responses: dict[tuple[str, str], str] = {}

    async def aclose(self) -> None:  # noqa: D401
        return

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        self.calls.append({"method": method, "path": path, "params": params, "json": json_body})
        return self.responses.get((method, path), {})

    async def request_text(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append({"method": method, "path": path, "params": params, "json": None})
        return self.text_responses.get((method, path), "")


@pytest.fixture
def client() -> FakeClient:
    c = FakeClient()
    set_client(c)
    yield c
    set_client(None)


@pytest.fixture
def server():
    return mcp


async def _call(server, name: str, **kwargs) -> Any:
    """Invoke a registered FastMCP tool by name. Mirrors the doppler harness."""
    tool = await server.get_tool(name)
    result = await tool.run(arguments=kwargs)
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


# --- read tools -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects_parses_response(client, server):
    client.responses[("GET", "/projects")] = {
        "projects": [
            {"id": "motto", "name": "Motto"},
            {"id": "scratch", "name": "Scratch"},
        ]
    }
    out = await _call(server, "list_projects")
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["id"] == "motto"
    assert client.calls == [{"method": "GET", "path": "/projects", "params": None, "json": None}]


@pytest.mark.asyncio
async def test_get_service_returns_dict(client, server):
    client.responses[("GET", "/projects/motto/services/sdr")] = {
        "service": {"id": "sdr", "name": "SDR Agent", "status": "running"}
    }
    out = await _call(server, "get_service", project_id="motto", service_id="sdr")
    assert out["id"] == "sdr"
    assert out["status"] == "running"


@pytest.mark.asyncio
async def test_list_jobs_handles_bare_list_response(client, server):
    # Defensive: some Northflank endpoints have returned bare arrays in the past.
    client.responses[("GET", "/projects/motto/jobs")] = [
        {"id": "auto-nudge", "schedule": "*/30 * * * *"},
    ]
    out = await _call(server, "list_jobs", project_id="motto")
    assert len(out) == 1
    assert out[0]["id"] == "auto-nudge"


@pytest.mark.asyncio
async def test_get_recent_logs_trims_to_lines(client, server):
    body = "\n".join(f"line-{i}" for i in range(500))
    client.text_responses[("GET", "/projects/motto/services/sdr/logs")] = body
    out = await _call(server, "get_recent_logs", project_id="motto", service_id="sdr", lines=10)
    assert out["project_id"] == "motto"
    assert out["service_id"] == "sdr"
    assert out["logs"].splitlines()[-1] == "line-499"
    assert len(out["logs"].splitlines()) == 10


# --- write safety ---------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_service_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(server, "restart_service", project_id="motto", service_id="sdr")
    # No HTTP call was made
    assert client.calls == []


@pytest.mark.asyncio
async def test_restart_service_calls_action_with_confirm(client, server):
    out = await _call(
        server,
        "restart_service",
        project_id="motto",
        service_id="sdr",
        confirm=True,
    )
    assert out == {
        "restarted": True,
        "project_id": "motto",
        "service_id": "sdr",
    }
    assert client.calls == [
        {
            "method": "POST",
            "path": "/projects/motto/services/sdr/actions/restart",
            "params": None,
            "json": None,
        }
    ]


@pytest.mark.asyncio
async def test_redeploy_service_requires_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(server, "redeploy_service", project_id="motto", service_id="sdr")
    assert client.calls == []


@pytest.mark.asyncio
async def test_trigger_job_run_with_confirm(client, server):
    out = await _call(
        server,
        "trigger_job_run",
        project_id="motto",
        job_id="auto-nudge",
        confirm=True,
    )
    assert out == {
        "triggered": True,
        "project_id": "motto",
        "job_id": "auto-nudge",
    }
    assert client.calls[0]["path"] == "/projects/motto/jobs/auto-nudge/actions/run"


@pytest.mark.asyncio
async def test_resync_secret_group_with_confirm(client, server):
    out = await _call(
        server,
        "resync_secret_group",
        project_id="motto",
        group_id="prd-shared",
        confirm=True,
    )
    assert out == {
        "resynced": True,
        "project_id": "motto",
        "group_id": "prd-shared",
    }


# --- auth contract --------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_token_raises_with_clear_message(monkeypatch):
    """A real client constructed without a token must explain itself."""
    monkeypatch.delenv("NORTHFLANK_API_TOKEN", raising=False)
    set_client(None)  # force a real-client construction on next call
    with pytest.raises(RuntimeError) as exc_info:
        nf.NorthflankClient()
    assert "NORTHFLANK_API_TOKEN" in str(exc_info.value)
    assert "Doppler" in str(exc_info.value)
