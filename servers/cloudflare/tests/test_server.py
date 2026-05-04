"""Unit tests for the Cloudflare MCP server.

We mock CloudflareClient so tests never touch the live API. The goal is to
verify the read parsing (Cloudflare's `{success, errors, result}` envelope
is unwrapped to `result`), the confirm-true safety gates on every mutator,
and the auth-error contract.
"""

from __future__ import annotations

from typing import Any

import pytest

from servers.cloudflare import server as cf
from servers.cloudflare.server import CloudflareClient, mcp, set_client


class FakeClient(CloudflareClient):
    """In-memory stand-in for CloudflareClient.

    Test code seeds ``responses[(method, path)]`` with whatever ``request()``
    should return *after* the Cloudflare envelope is unwrapped — i.e. the
    contents of the ``result`` field. To simulate an error path, raise from
    a populated entry by storing an Exception instance instead.
    """

    def __init__(self) -> None:  # type: ignore[override]
        # Skip parent __init__ — we don't want to require CLOUDFLARE_API_TOKEN.
        self._token = "fake"
        self._client = None  # type: ignore[assignment]
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, str], Any] = {}

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
        value = self.responses.get((method, path))
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def client() -> FakeClient:
    c = FakeClient()
    set_client(c)
    yield c
    set_client(None)


@pytest.fixture
def server():
    return mcp


async def _call(server, _tool_name: str, **kwargs) -> Any:
    """Invoke a registered FastMCP tool by name. Mirrors the doppler/northflank
    harness; unwraps FastMCP's ``{"result": [...]}`` envelope for list returns.

    The first positional is renamed ``_tool_name`` so tests can pass a tool
    arg literally named ``name`` (Cloudflare DNS records have a ``name`` field).
    """
    tool = await server.get_tool(_tool_name)
    result = await tool.run(arguments=kwargs)
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


# --- read tools -----------------------------------------------------------


@pytest.mark.asyncio
async def test_list_accounts_parses_response(client, server):
    client.responses[("GET", "/accounts")] = [
        {"id": "acc1", "name": "Motto"},
        {"id": "acc2", "name": "Scratch"},
    ]
    out = await _call(server, "list_accounts")
    assert isinstance(out, list)
    assert len(out) == 2
    assert out[0]["id"] == "acc1"
    assert client.calls == [{"method": "GET", "path": "/accounts", "params": None, "json": None}]


@pytest.mark.asyncio
async def test_list_zones_without_account_filter(client, server):
    client.responses[("GET", "/zones")] = [
        {"id": "z1", "name": "motto.app"},
    ]
    out = await _call(server, "list_zones")
    assert len(out) == 1
    # No account.id filter when account_id is omitted.
    assert client.calls[0]["params"] is None


@pytest.mark.asyncio
async def test_list_zones_with_account_filter(client, server):
    client.responses[("GET", "/zones")] = [
        {"id": "z1", "name": "motto.app"},
    ]
    out = await _call(server, "list_zones", account_id="acc1")
    assert len(out) == 1
    assert client.calls[0]["params"] == {"account.id": "acc1"}


@pytest.mark.asyncio
async def test_list_dns_records_passes_filters(client, server):
    client.responses[("GET", "/zones/z1/dns_records")] = [
        {"id": "r1", "type": "A", "name": "www.motto.app"},
    ]
    out = await _call(server, "list_dns_records", zone_id="z1", type="A", name="www.motto.app")
    assert len(out) == 1
    assert client.calls[0]["params"] == {"type": "A", "name": "www.motto.app"}


@pytest.mark.asyncio
async def test_list_workers_returns_list(client, server):
    client.responses[("GET", "/accounts/acc1/workers/scripts")] = [
        {"id": "edge-router", "modified_on": "2026-05-04T00:00:00Z"},
    ]
    out = await _call(server, "list_workers", account_id="acc1")
    assert len(out) == 1
    assert out[0]["id"] == "edge-router"


@pytest.mark.asyncio
async def test_list_r2_buckets_unwraps_buckets_key(client, server):
    """R2 returns ``{"buckets": [...]}`` inside ``result``; tool normalizes."""
    client.responses[("GET", "/accounts/acc1/r2/buckets")] = {
        "buckets": [
            {"name": "motto-uploads", "creation_date": "2026-04-01T00:00:00Z"},
            {"name": "motto-archive", "creation_date": "2026-03-15T00:00:00Z"},
        ]
    }
    out = await _call(server, "list_r2_buckets", account_id="acc1")
    assert len(out) == 2
    assert {b["name"] for b in out} == {"motto-uploads", "motto-archive"}


# --- write safety ---------------------------------------------------------


@pytest.mark.asyncio
async def test_create_dns_record_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(
            server,
            "create_dns_record",
            zone_id="z1",
            type="A",
            name="staging",
            content="203.0.113.42",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_create_dns_record_with_confirm_posts_payload(client, server):
    client.responses[("POST", "/zones/z1/dns_records")] = {
        "id": "r-new",
        "type": "A",
        "name": "staging.motto.app",
        "content": "203.0.113.42",
        "proxied": True,
    }
    out = await _call(
        server,
        "create_dns_record",
        zone_id="z1",
        type="A",
        name="staging",
        content="203.0.113.42",
        proxied=True,
        confirm=True,
    )
    assert out["id"] == "r-new"
    assert client.calls[0]["method"] == "POST"
    assert client.calls[0]["path"] == "/zones/z1/dns_records"
    body = client.calls[0]["json"]
    assert body["type"] == "A"
    assert body["name"] == "staging"
    assert body["content"] == "203.0.113.42"
    assert body["proxied"] is True
    assert body["ttl"] == 1


@pytest.mark.asyncio
async def test_purge_zone_cache_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(server, "purge_zone_cache", zone_id="z1")
    assert client.calls == []


@pytest.mark.asyncio
async def test_purge_zone_cache_with_confirm(client, server):
    client.responses[("POST", "/zones/z1/purge_cache")] = {"id": "z1"}
    out = await _call(server, "purge_zone_cache", zone_id="z1", confirm=True)
    assert out == {"purged": True, "zone_id": "z1"}
    assert client.calls[0]["json"] == {"purge_everything": True}


@pytest.mark.asyncio
async def test_delete_dns_record_with_confirm(client, server):
    client.responses[("DELETE", "/zones/z1/dns_records/r1")] = {"id": "r1"}
    out = await _call(
        server,
        "delete_dns_record",
        zone_id="z1",
        record_id="r1",
        confirm=True,
    )
    assert out == {"deleted": True, "zone_id": "z1", "record_id": "r1"}
    assert client.calls[0]["method"] == "DELETE"
    assert client.calls[0]["path"] == "/zones/z1/dns_records/r1"


@pytest.mark.asyncio
async def test_redeploy_pages_refuses_without_confirm(client, server):
    with pytest.raises(RuntimeError, match="confirm=False"):
        await _call(
            server,
            "redeploy_pages",
            account_id="acc1",
            project_name="motto-cockpit",
        )
    assert client.calls == []


@pytest.mark.asyncio
async def test_redeploy_pages_with_confirm_posts_branch(client, server):
    client.responses[("POST", "/accounts/acc1/pages/projects/motto-cockpit/deployments")] = {
        "id": "dep-1",
        "environment": "production",
    }
    out = await _call(
        server,
        "redeploy_pages",
        account_id="acc1",
        project_name="motto-cockpit",
        branch="main",
        confirm=True,
    )
    assert out["id"] == "dep-1"
    assert client.calls[0]["json"] == {"branch": "main"}


# --- error envelope -------------------------------------------------------


@pytest.mark.asyncio
async def test_cloudflare_error_envelope_propagates_messages(server):
    """When CloudflareClient raises (e.g. CF returned success:false), the
    error message must surface via the tool — callers shouldn't have to
    guess what failed."""
    cf.set_client(None)

    class ErroringClient(CloudflareClient):
        def __init__(self):  # type: ignore[override]
            self._token = "fake"
            self._client = None  # type: ignore[assignment]

        async def request(self, *_, **__):
            raise RuntimeError("Cloudflare GET /zones/bad failed [200]: [7003] Could not route")

    cf.set_client(ErroringClient())
    try:
        with pytest.raises(RuntimeError, match=r"\[7003\]"):
            await _call(server, "get_zone", zone_id="bad")
    finally:
        cf.set_client(None)


# --- auth contract --------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_token_raises_with_clear_message(monkeypatch):
    """A real client constructed without a token must explain itself."""
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    set_client(None)  # force a real-client construction on next call
    with pytest.raises(RuntimeError) as exc_info:
        cf.CloudflareClient()
    assert "CLOUDFLARE_API_TOKEN" in str(exc_info.value)
    assert "Doppler" in str(exc_info.value)
