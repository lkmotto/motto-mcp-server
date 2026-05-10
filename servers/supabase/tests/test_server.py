"""Unit tests for the Supabase MCP server.

The most important assertions live in the SELECT-only enforcement: any
write/DDL statement must be refused before it reaches Supabase.
"""

from __future__ import annotations

from typing import Any

import pytest

from servers.supabase import server as sb
from servers.supabase.server import SupabaseClient, mcp, set_client


class FakeClient(SupabaseClient):
    def __init__(self) -> None:  # type: ignore[override]
        self._url = "https://fake.supabase.co"
        self._service_key = "fake"
        self._client = None  # type: ignore[assignment]
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, str], Any] = {}

    async def aclose(self) -> None:
        return

    async def rpc(self, name: str, args: dict[str, Any]) -> Any:
        self.calls.append({"name": name, "args": args})
        return self.responses.get(("rpc", name), [])

    async def from_table(
        self, table: str, *, select: str = "*", limit: int = 1000
    ) -> Any:
        self.calls.append({"table": table, "select": select, "limit": limit})
        return self.responses.get(("table", table), [])


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


# --- SELECT-only enforcement ---------------------------------------------


@pytest.mark.parametrize(
    "bad_sql",
    [
        "INSERT INTO users (id) VALUES (1)",
        "UPDATE users SET name = 'x' WHERE id = 1",
        "DELETE FROM users WHERE id = 1",
        "DROP TABLE users",
        "CREATE TABLE x (id int)",
        "ALTER TABLE users ADD COLUMN x int",
        "TRUNCATE users",
        "GRANT ALL ON users TO anon",
        "SELECT 1; DELETE FROM users",
        "SELECT * FROM users; SELECT 1",
        "  ",
        "EXEC sp_who",
        "MERGE INTO users USING staged ON 1=1 WHEN MATCHED THEN UPDATE SET name='x'",
    ],
)
def test_enforce_select_only_rejects(bad_sql):
    with pytest.raises(ValueError):
        sb._enforce_select_only(bad_sql)


@pytest.mark.parametrize(
    "ok_sql",
    [
        "SELECT 1",
        "select id, name from users where id = 1",
        "WITH t AS (SELECT 1) SELECT * FROM t",
        "SELECT * FROM users -- INSERT INTO ignored",
        "SELECT * FROM users /* DROP TABLE ignored */",
    ],
)
def test_enforce_select_only_accepts(ok_sql):
    out = sb._enforce_select_only(ok_sql)
    assert isinstance(out, str)
    assert out


# --- query tool -----------------------------------------------------------


@pytest.mark.asyncio
async def test_query_appends_limit_when_missing(client, server):
    client.responses[("rpc", "motto_query")] = [{"id": 1}]
    out = await _call(server, "query", sql="SELECT id FROM users", max_rows=50)
    assert out["row_count"] == 1
    args = client.calls[0]["args"]
    assert args["sql"].endswith("LIMIT 50")
    assert args["max_rows"] == 50


@pytest.mark.asyncio
async def test_query_keeps_existing_limit(client, server):
    client.responses[("rpc", "motto_query")] = [{"id": 1}, {"id": 2}]
    out = await _call(
        server, "query", sql="SELECT id FROM users LIMIT 10", max_rows=50
    )
    assert out["row_count"] == 2
    args = client.calls[0]["args"]
    assert args["sql"].endswith("LIMIT 10")


@pytest.mark.asyncio
async def test_query_refuses_writes_at_tool_boundary(client, server):
    with pytest.raises(ValueError):
        await _call(server, "query", sql="DELETE FROM users")
    assert client.calls == []


# --- list_tables / describe_table ----------------------------------------


@pytest.mark.asyncio
async def test_list_tables_invokes_rpc(client, server):
    client.responses[("rpc", "motto_query")] = [
        {"table_name": "users", "table_type": "BASE TABLE"}
    ]
    out = await _call(server, "list_tables", schema="public")
    assert out[0]["table_name"] == "users"
    sql = client.calls[0]["args"]["sql"]
    assert "information_schema.tables" in sql
    assert "table_schema = 'public'" in sql


@pytest.mark.asyncio
async def test_describe_table_invokes_rpc(client, server):
    client.responses[("rpc", "motto_query")] = [
        {"column_name": "id", "data_type": "uuid"}
    ]
    out = await _call(server, "describe_table", table="users", schema="public")
    assert out[0]["column_name"] == "id"
    sql = client.calls[0]["args"]["sql"]
    assert "information_schema.columns" in sql
    assert "table_name = 'users'" in sql


@pytest.mark.asyncio
async def test_describe_table_rejects_bad_identifier(client, server):
    with pytest.raises(ValueError):
        await _call(server, "describe_table", table="users; DROP TABLE x")


# --- auth contract --------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_creds_raise_with_clear_message(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_SECRET_KEY", raising=False)
    set_client(None)
    with pytest.raises(RuntimeError) as exc_info:
        sb.SupabaseClient()
    msg = str(exc_info.value)
    assert "SUPABASE_URL" in msg
    assert "Doppler" in msg
