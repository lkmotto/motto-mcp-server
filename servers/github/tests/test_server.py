"""Unit tests for the GitHub MCP server.

We mock GitHubClient so tests never touch the live API. The goal is to
verify read parsing, the confirm-true safety gates, and the auth-error
contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from servers.github import server as gh
from servers.github.server import GitHubClient, mcp, set_client


class FakeClient(GitHubClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._token = "fake"
        self._client = None  # type: ignore[assignment]
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, str], Any] = {}

    async def aclose(self) -> None:
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
async def test_list_repos_user_default(client, server):
    client.responses[("GET", "/user/repos")] = [
        {"id": 1, "name": "motto-mcp-server", "full_name": "lkmotto/motto-mcp-server"},
    ]
    out = await _call(server, "list_repos")
    assert isinstance(out, list)
    assert out[0]["name"] == "motto-mcp-server"
    call = client.calls[0]
    assert call["path"] == "/user/repos"
    assert call["params"]["affiliation"] == "owner,collaborator,organization_member"


@pytest.mark.asyncio
async def test_list_repos_for_org(client, server):
    client.responses[("GET", "/orgs/lkmotto/repos")] = [{"name": "motto-sdr-agent"}]
    out = await _call(server, "list_repos", owner="lkmotto", per_page=10)
    assert out[0]["name"] == "motto-sdr-agent"
    assert client.calls[0]["params"] == {"per_page": 10}


@pytest.mark.asyncio
async def test_get_repo_returns_dict(client, server):
    client.responses[("GET", "/repos/lkmotto/motto-mcp-server")] = {
        "id": 1,
        "name": "motto-mcp-server",
        "default_branch": "main",
    }
    out = await _call(server, "get_repo", owner="lkmotto", repo="motto-mcp-server")
    assert out["default_branch"] == "main"


@pytest.mark.asyncio
async def test_list_issues_passes_filters(client, server):
    client.responses[("GET", "/repos/lkmotto/motto-mcp-server/issues")] = [
        {"number": 7, "title": "Migrate to Doppler"},
    ]
    out = await _call(
        server, "list_issues",
        owner="lkmotto", repo="motto-mcp-server",
        state="open", labels="provisioner",
    )
    assert out[0]["number"] == 7
    params = client.calls[0]["params"]
    assert params["state"] == "open"
    assert params["labels"] == "provisioner"


@pytest.mark.asyncio
async def test_list_pulls_returns_list(client, server):
    client.responses[("GET", "/repos/lkmotto/motto-mcp-server/pulls")] = [
        {"number": 12, "title": "Mount cloudflare + northflank"},
    ]
    out = await _call(server, "list_pulls", owner="lkmotto", repo="motto-mcp-server")
    assert out[0]["number"] == 12


# --- write safety ---------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(
            server, "create_issue",
            owner="lkmotto", repo="motto-mcp-server", title="hi",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_create_issue_with_confirm(client, server):
    client.responses[("POST", "/repos/lkmotto/motto-mcp-server/issues")] = {
        "number": 99,
        "title": "hi",
    }
    out = await _call(
        server, "create_issue",
        owner="lkmotto", repo="motto-mcp-server",
        title="hi", body="from test",
        labels=["test"], assignees=["ljm32901"],
        confirm=True,
    )
    assert out["number"] == 99
    payload = client.calls[0]["json"]
    assert payload == {
        "title": "hi",
        "body": "from test",
        "labels": ["test"],
        "assignees": ["ljm32901"],
    }


@pytest.mark.asyncio
async def test_comment_issue_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(
            server, "comment_issue",
            owner="lkmotto", repo="motto-mcp-server",
            issue_number=15, body="hello",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_comment_issue_with_confirm(client, server):
    client.responses[("POST", "/repos/lkmotto/motto-mcp-server/issues/15/comments")] = {
        "id": 4242,
        "body": "hello",
    }
    out = await _call(
        server, "comment_issue",
        owner="lkmotto", repo="motto-mcp-server",
        issue_number=15, body="hello", confirm=True,
    )
    assert out["id"] == 4242


@pytest.mark.asyncio
async def test_create_pull_with_confirm(client, server):
    client.responses[("POST", "/repos/lkmotto/motto-mcp-server/pulls")] = {
        "number": 41,
        "html_url": "https://github.com/lkmotto/motto-mcp-server/pull/41",
    }
    out = await _call(
        server, "create_pull",
        owner="lkmotto", repo="motto-mcp-server",
        title="MOT-15", head="mot-15", base="main",
        body="part A", draft=True, confirm=True,
    )
    assert out["number"] == 41
    payload = client.calls[0]["json"]
    assert payload == {
        "title": "MOT-15",
        "head": "mot-15",
        "base": "main",
        "draft": True,
        "body": "part A",
    }


@pytest.mark.asyncio
async def test_merge_pull_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(
            server, "merge_pull",
            owner="lkmotto", repo="motto-mcp-server", pull_number=41,
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_merge_pull_with_confirm(client, server):
    client.responses[("PUT", "/repos/lkmotto/motto-mcp-server/pulls/41/merge")] = {
        "merged": True,
        "sha": "deadbeef",
    }
    out = await _call(
        server, "merge_pull",
        owner="lkmotto", repo="motto-mcp-server",
        pull_number=41, confirm=True,
    )
    assert out["merged"] is True
    assert client.calls[0]["json"] == {"merge_method": "squash"}


@pytest.mark.asyncio
async def test_set_secret_refuses_without_confirm(client, server):
    tool = await server.get_tool("set_secret")
    with pytest.raises(RuntimeError, match="confirm=False"):
        await tool.run(
            arguments={
                "owner": "lkmotto",
                "repo": "motto-mcp-server",
                "name": "X",
                "value": "y",
            }
        )
    assert client.calls == []


# --- auth contract --------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_token_raises_with_clear_message(monkeypatch):
    monkeypatch.delenv("FLEET_PROVISION_PAT", raising=False)
    monkeypatch.delenv("GITHUB_PAT", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    set_client(None)
    with pytest.raises(RuntimeError) as exc_info:
        gh.GitHubClient()
    msg = str(exc_info.value)
    assert "FLEET_PROVISION_PAT" in msg
    assert "Doppler" in msg
