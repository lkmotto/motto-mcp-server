"""Unit tests for the Linear MCP server.

We mock LinearClient so tests never touch the live API.
"""

from __future__ import annotations

from typing import Any

import pytest

from servers.linear import server as ln
from servers.linear.server import LinearClient, mcp, set_client


class FakeClient(LinearClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._api_key = "fake"
        self._client = None  # type: ignore[assignment]
        self.calls: list[dict[str, Any]] = []
        self.responses: list[dict[str, Any]] = []

    async def aclose(self) -> None:
        return

    async def query(
        self,
        graphql: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"graphql": graphql, "variables": variables})
        if self.responses:
            return self.responses.pop(0)
        return {}


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
    tool = await server.get_tool(name)
    result = await tool.run(arguments=kwargs)
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


# --- read tools -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_issues_unwraps_nodes(client, server):
    client.responses = [
        {
            "issues": {
                "nodes": [
                    {"id": "abc", "identifier": "MOT-15", "title": "Bridge"},
                ]
            }
        }
    ]
    out = await _call(server, "list_issues", team_key="MOT", first=10)
    assert isinstance(out, list)
    assert out[0]["identifier"] == "MOT-15"
    vars_ = client.calls[0]["variables"]
    assert vars_["first"] == 10
    assert vars_["filter"] == {"team": {"key": {"eq": "MOT"}}}


@pytest.mark.asyncio
async def test_list_issues_no_filters_passes_none(client, server):
    client.responses = [{"issues": {"nodes": []}}]
    out = await _call(server, "list_issues")
    assert out == []
    assert client.calls[0]["variables"]["filter"] is None


@pytest.mark.asyncio
async def test_get_issue_returns_dict(client, server):
    client.responses = [{"issue": {"id": "abc", "identifier": "MOT-15"}}]
    out = await _call(server, "get_issue", id="MOT-15")
    assert out["identifier"] == "MOT-15"
    assert client.calls[0]["variables"] == {"id": "MOT-15"}


@pytest.mark.asyncio
async def test_list_projects_returns_nodes(client, server):
    client.responses = [
        {"projects": {"nodes": [{"id": "p1", "name": "Fleet Operations"}]}}
    ]
    out = await _call(server, "list_projects", first=5)
    assert out[0]["name"] == "Fleet Operations"


# --- write safety ---------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(
            server, "create_issue",
            team_id="team-uuid", title="hi",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_create_issue_with_confirm(client, server):
    client.responses = [
        {
            "issueCreate": {
                "success": True,
                "issue": {"id": "i1", "identifier": "MOT-99", "title": "hi"},
            }
        }
    ]
    out = await _call(
        server, "create_issue",
        team_id="team-uuid", title="hi",
        description="body", priority=2, confirm=True,
    )
    assert out["success"] is True
    assert out["issue"]["identifier"] == "MOT-99"
    payload = client.calls[0]["variables"]["input"]
    assert payload == {
        "teamId": "team-uuid",
        "title": "hi",
        "description": "body",
        "priority": 2,
    }


@pytest.mark.asyncio
async def test_update_issue_requires_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(server, "update_issue", id="MOT-15", title="x")
    assert client.calls == []


@pytest.mark.asyncio
async def test_update_issue_no_op_when_no_fields(client, server):
    out = await _call(server, "update_issue", id="MOT-15", confirm=True)
    assert out["success"] is False
    assert client.calls == []


@pytest.mark.asyncio
async def test_create_comment_with_confirm(client, server):
    client.responses = [
        {"commentCreate": {"success": True, "comment": {"id": "c1", "body": "hi"}}}
    ]
    out = await _call(
        server, "create_comment",
        issue_id="MOT-15", body="hi", confirm=True,
    )
    assert out["success"] is True
    payload = client.calls[0]["variables"]["input"]
    assert payload == {"issueId": "MOT-15", "body": "hi"}


# --- auth contract --------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_key_raises_with_clear_message(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    set_client(None)
    with pytest.raises(RuntimeError) as exc_info:
        ln.LinearClient()
    msg = str(exc_info.value)
    assert "LINEAR_API_KEY" in msg
    assert "Doppler" in msg
