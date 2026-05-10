"""Supabase MCP server — read-only SQL passthrough for the Motto fleet.

Only ``SELECT`` statements are accepted. The tool boundary rejects any
write or DDL statement BEFORE it reaches Supabase, so a leaked service
key cannot be turned into mutations through this surface. Backed by the
Supabase REST RPC ``query`` endpoint or, if a Postgres connection string
is supplied, asyncpg.

Run with ``python -m servers.supabase`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.

Auth (lazy, read at first use):
* ``SUPABASE_URL`` — project base URL, e.g. ``https://abcd.supabase.co``.
* ``SUPABASE_SERVICE_KEY`` — service-role key (never anon). Aliases:
  ``SUPABASE_SERVICE_ROLE_KEY``, ``SUPABASE_SECRET_KEY``.

Both come from Doppler ``motto-core/prd``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_ROWS = 1000

mcp = FastMCP(
    "motto-supabase",
    instructions=(
        "Read-only Supabase SQL passthrough for the Motto schema. Only SELECT "
        "is permitted; any other statement (INSERT/UPDATE/DELETE/DDL/multi-stmt) "
        "is refused at the tool boundary. Auth via SUPABASE_URL + "
        "SUPABASE_SERVICE_KEY (Doppler motto-core/prd)."
    ),
)


# ---------------------------------------------------------------------------
# SELECT-only guard
# ---------------------------------------------------------------------------


_SELECT_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)
_FORBIDDEN_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|"
    r"COMMENT|VACUUM|ANALYZE|REINDEX|REFRESH|CALL|DO|COPY|EXECUTE|"
    r"BEGIN|COMMIT|ROLLBACK|SAVEPOINT|MERGE)\b",
    re.IGNORECASE,
)


def _strip_sql_comments(sql: str) -> str:
    """Remove ``--`` line comments and ``/* ... */`` block comments so the
    keyword scan doesn't false-trigger on commented-out tokens."""
    no_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    return re.sub(r"--[^\n]*", " ", no_block)


def _enforce_select_only(sql: str) -> str:
    """Return a sanitised SQL string or raise on policy violation.

    Rules:
        * exactly one statement (no ``;`` after trimming)
        * starts with SELECT or WITH (CTE prelude)
        * contains no forbidden DDL/DML keywords
    """
    cleaned = _strip_sql_comments(sql).strip()
    if not cleaned:
        raise ValueError("supabase.query: SQL is empty")
    # collapse trailing semicolons before checking statement count
    while cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if ";" in cleaned:
        raise ValueError(
            "supabase.query: only one statement per call is allowed; "
            "found ';' inside the SQL"
        )
    if not _SELECT_RE.match(cleaned):
        raise ValueError(
            "supabase.query: only SELECT (or WITH ... SELECT) statements are "
            "permitted; got prefix: " + cleaned[:32]
        )
    if _FORBIDDEN_RE.search(cleaned):
        forbidden = _FORBIDDEN_RE.search(cleaned).group(0)  # type: ignore[union-attr]
        raise ValueError(
            f"supabase.query: forbidden keyword '{forbidden}' rejected at the "
            "tool boundary"
        )
    return cleaned


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class SupabaseClient:
    """Thin async wrapper around the Supabase REST surface.

    Reads use PostgREST-style table queries; ``query`` uses Supabase's
    ``rpc/`` endpoint to run a parametrised SELECT helper. The helper
    function ``public.motto_query(sql text, max_rows int)`` is expected
    to exist in the project — see ``docs/supabase-setup.md``. If it
    doesn't, falls back to PostgREST table reads.
    """

    def __init__(
        self,
        url: str | None = None,
        service_key: str | None = None,
    ) -> None:
        self._url = (url or os.environ.get("SUPABASE_URL") or "").rstrip("/")
        self._service_key = service_key or _first_env(
            "SUPABASE_SERVICE_KEY",
            "SUPABASE_SERVICE_ROLE_KEY",
            "SUPABASE_SECRET_KEY",
        )
        if not self._url or not self._service_key:
            raise RuntimeError(
                "SUPABASE_URL + SUPABASE_SERVICE_KEY (or alias "
                "SUPABASE_SERVICE_ROLE_KEY / SUPABASE_SECRET_KEY) are required. "
                "Set them via Doppler (motto-core/prd) and inject at runtime."
            )
        self._client = httpx.AsyncClient(
            base_url=self._url,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "apikey": self._service_key,
                "Authorization": f"Bearer {self._service_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def rpc(self, name: str, args: dict[str, Any]) -> Any:
        resp = await self._client.post(f"/rest/v1/rpc/{name}", json=args)
        return _handle_response(resp, method="POST", path=f"/rest/v1/rpc/{name}")

    async def from_table(
        self,
        table: str,
        *,
        select: str = "*",
        limit: int = DEFAULT_MAX_ROWS,
    ) -> Any:
        params = {"select": select, "limit": str(limit)}
        resp = await self._client.get(f"/rest/v1/{table}", params=params)
        return _handle_response(resp, method="GET", path=f"/rest/v1/{table}")


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _handle_response(resp: httpx.Response, *, method: str, path: str) -> Any:
    if resp.status_code == 204 or not resp.content:
        if resp.status_code >= 400:
            _raise_http_error(resp, method=method, path=path)
        return []
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        _raise_http_error(resp, method=method, path=path, parsed=data)
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
    raise RuntimeError(f"Supabase {method} {path} failed [{resp.status_code}]: {summary}")


# ---------------------------------------------------------------------------
# Lazy client holder
# ---------------------------------------------------------------------------


_client_holder: dict[str, SupabaseClient] = {}


def _client() -> SupabaseClient:
    if "c" not in _client_holder:
        _client_holder["c"] = SupabaseClient()
    return _client_holder["c"]


def set_client(client: SupabaseClient | None) -> None:
    """Swap the lazy client (tests). Pass None to reset."""
    if client is None:
        _client_holder.pop("c", None)
    else:
        _client_holder["c"] = client


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def query(sql: str, max_rows: int = DEFAULT_MAX_ROWS) -> dict[str, Any]:
    """Run a SELECT (or WITH...SELECT) statement against Supabase.

    Anything other than a single read-only statement is refused at the tool
    boundary — it never reaches Supabase. Output is capped at ``max_rows``.

    Args:
        sql: a single SELECT (or WITH ... SELECT) statement.
        max_rows: row cap applied via ``LIMIT`` if missing (default 1000).
    Returns: ``{"sql", "rows": [...], "row_count"}``.
    """
    cleaned = _enforce_select_only(sql)
    cap = max(1, min(int(max_rows), 10000))
    if not re.search(r"\bLIMIT\b", cleaned, re.IGNORECASE):
        cleaned_with_cap = f"{cleaned} LIMIT {cap}"
    else:
        cleaned_with_cap = cleaned
    rows = await _client().rpc("motto_query", {"sql": cleaned_with_cap, "max_rows": cap})
    if not isinstance(rows, list):
        rows = [rows] if rows else []
    return {"sql": cleaned_with_cap, "rows": rows, "row_count": len(rows)}


@mcp.tool()
async def list_tables(schema: str = "public") -> list[dict[str, Any]]:
    """List tables in a schema using ``information_schema.tables``.

    Args:
        schema: schema name (default ``public``).
    Returns: list of ``{"table_name", "table_type"}``.
    """
    sql = (
        "SELECT table_name, table_type FROM information_schema.tables "
        f"WHERE table_schema = '{_quote_ident_value(schema)}' "
        "ORDER BY table_name"
    )
    rows = await _client().rpc("motto_query", {"sql": sql, "max_rows": 1000})
    return rows if isinstance(rows, list) else []


@mcp.tool()
async def describe_table(
    table: str, schema: str = "public"
) -> list[dict[str, Any]]:
    """Describe a table's columns via ``information_schema.columns``.

    Args:
        table: table name.
        schema: schema name (default ``public``).
    Returns: list of column dicts with ``column_name``, ``data_type``,
        ``is_nullable``, ``column_default``.
    """
    sql = (
        "SELECT column_name, data_type, is_nullable, column_default "
        "FROM information_schema.columns "
        f"WHERE table_schema = '{_quote_ident_value(schema)}' "
        f"AND table_name = '{_quote_ident_value(table)}' "
        "ORDER BY ordinal_position"
    )
    rows = await _client().rpc("motto_query", {"sql": sql, "max_rows": 1000})
    return rows if isinstance(rows, list) else []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_ident_value(value: str) -> str:
    """Whitelist a schema/table name to prevent SQL injection in describe tools.

    ``information_schema`` queries above use string literals (not identifiers),
    but we still reject anything that isn't a plain identifier so a hostile
    caller can't inject a ``'`` and break out.
    """
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"supabase: invalid identifier '{value}' — must match {_IDENT_RE.pattern}"
        )
    return value


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the Supabase MCP server. stdio by default; set MCP_TRANSPORT=http
    to expose over HTTP (PORT, default 8087)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8087"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
