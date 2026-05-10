"""Unit tests for the Apollo MCP server."""

from __future__ import annotations

from typing import Any

import pytest

from servers.apollo import server as ap
from servers.apollo.server import ApolloClient, mcp, set_client


class FakeClient(ApolloClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._api_key = "fake"
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
async def test_search_people_passes_filters(client, server):
    client.responses[("POST", "/mixed_people/search")] = {
        "people": [{"id": "p1", "name": "Jane"}],
        "pagination": {"page": 1},
    }
    out = await _call(
        server, "search_people",
        person_titles=["Director of Appraisal"],
        organization_locations=["Texas, US"],
        page=1, per_page=10,
    )
    assert out["people"][0]["id"] == "p1"
    body = client.calls[0]["json"]
    assert body["page"] == 1
    assert body["per_page"] == 10
    assert body["person_titles"] == ["Director of Appraisal"]
    assert body["organization_locations"] == ["Texas, US"]


@pytest.mark.asyncio
async def test_enrich_person_with_email(client, server):
    client.responses[("POST", "/people/match")] = {
        "person": {"id": "p1", "email": "j@example.com"}
    }
    out = await _call(server, "enrich_person", email="j@example.com")
    assert out["person"]["id"] == "p1"
    body = client.calls[0]["json"]
    assert body["email"] == "j@example.com"
    assert body["reveal_personal_emails"] is False


@pytest.mark.asyncio
async def test_list_sequences_passes_pagination(client, server):
    client.responses[("GET", "/emailer_campaigns/search")] = {
        "emailer_campaigns": [{"id": "s1"}]
    }
    out = await _call(server, "list_sequences", page=2, per_page=50)
    assert out["emailer_campaigns"][0]["id"] == "s1"
    assert client.calls[0]["params"] == {"page": 2, "per_page": 50}


# --- write safety ---------------------------------------------------------


@pytest.mark.asyncio
async def test_add_to_sequence_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(
            server, "add_to_sequence",
            sequence_id="s1", contact_ids=["c1", "c2"],
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_add_to_sequence_with_confirm(client, server):
    client.responses[("POST", "/emailer_campaigns/s1/add_contact_ids")] = {
        "contacts": [{"id": "c1"}, {"id": "c2"}]
    }
    out = await _call(
        server, "add_to_sequence",
        sequence_id="s1", contact_ids=["c1", "c2"],
        send_email_from_email_account_id="acct-1",
        confirm=True,
    )
    assert len(out["contacts"]) == 2
    body = client.calls[0]["json"]
    assert body["emailer_campaign_id"] == "s1"
    assert body["contact_ids"] == ["c1", "c2"]
    assert body["send_email_from_email_account_id"] == "acct-1"


# --- auth contract --------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_key_raises_with_clear_message(monkeypatch):
    monkeypatch.delenv("APOLLO_API_KEY", raising=False)
    set_client(None)
    with pytest.raises(RuntimeError) as exc_info:
        ap.ApolloClient()
    msg = str(exc_info.value)
    assert "APOLLO_API_KEY" in msg
    assert "Doppler" in msg
