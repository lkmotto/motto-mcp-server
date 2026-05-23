"""Tests for create_epic + dispatch_droid_for_epic MCP tools (Worker A).

Both tools call external HTTP services (GitHub Issues API + Factory API);
httpx is monkey-patched so the tests exercise the DB plumbing without
hitting the network. The public.epics table is owned by motto-director
migration 0006_epics.sql -- tests skip when the table is missing in the
target DB, mirroring the test_claim_next_step pattern.
"""

from __future__ import annotations

import json as _json
from typing import Any

import pytest

from tests.conftest import _call, requires_db

pytestmark = pytest.mark.asyncio


async def _epics_table_exists(db) -> bool:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'epics'
            )
            """
        )


async def _wipe(db) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM public.epics WHERE title LIKE 'worker-a test%'"
        )


@pytest.fixture
async def epics_db(db):
    if not await _epics_table_exists(db):
        pytest.skip("public.epics not present in test DB")
    await _wipe(db)
    yield db
    await _wipe(db)


class _FakeResponse:
    def __init__(self, status: int, body: dict[str, Any] | None = None):
        self.status_code = status
        self._body = body or {}
        self.text = str(body)

    def json(self) -> dict[str, Any]:
        return self._body


class _FakeAsyncClient:
    """Records GitHub / Factory requests and returns scripted responses."""

    def __init__(
        self,
        calls: list[tuple[str, str, dict[str, Any]]],
        responses: list[_FakeResponse],
    ):
        self._calls = calls
        self._responses = responses

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    async def post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> _FakeResponse:
        self._calls.append(("POST", url, json or {}))
        return self._responses.pop(0)

    async def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> _FakeResponse:
        self._calls.append(("GET", url, {}))
        return self._responses.pop(0)


def _patch_httpx(monkeypatch, calls: list, responses: list[_FakeResponse]) -> None:
    from mcp_server.tools import epics as epics_mod

    def _ctor(*_args, **_kwargs):
        return _FakeAsyncClient(calls, responses)

    monkeypatch.setattr(epics_mod.httpx, "AsyncClient", _ctor)


# ---------------------------------------------------------------------------
# create_epic
# ---------------------------------------------------------------------------


@requires_db
async def test_create_epic_files_issue_and_inserts_row(epics_db, server, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [
            _FakeResponse(
                201,
                {
                    "number": 9999,
                    "html_url": "https://github.com/lkmotto/motto-mcp-server/issues/9999",
                },
            )
        ],
    )

    result = await _call(
        server,
        "create_epic",
        title="worker-a test alpha",
        repo_full_name="lkmotto/motto-mcp-server",
        body="some body text",
        labels=["bug"],
        success_criteria=["c1", "c2"],
        max_cost_usd=10.0,
        max_hours=4,
    )

    assert result["issue_number"] == 9999
    assert result["issue_url"].endswith("/issues/9999")
    assert result["status"] == "proposed"
    assert isinstance(result["epic_id"], int)

    assert len(calls) == 1
    method, url, body = calls[0]
    assert method == "POST"
    assert url == "https://api.github.com/repos/lkmotto/motto-mcp-server/issues"
    assert set(body["labels"]) == {"epic", "bug"}

    async with epics_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT title, status, gh_issue_url, gh_issue_number, max_cost_usd, "
            "max_hours, success_criteria_json FROM public.epics WHERE id = $1",
            result["epic_id"],
        )
    assert row["title"] == "worker-a test alpha"
    assert row["status"] == "proposed"
    assert row["gh_issue_number"] == 9999
    assert float(row["max_cost_usd"]) == 10.0
    assert row["max_hours"] == 4
    sc = row["success_criteria_json"]
    if isinstance(sc, str):
        sc = _json.loads(sc)
    assert sc == ["c1", "c2"]


@requires_db
async def test_create_epic_human_filed_starts_active(epics_db, server, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    _patch_httpx(
        monkeypatch,
        [],
        [_FakeResponse(201, {"number": 1, "html_url": "https://example.com/1"})],
    )

    result = await _call(
        server,
        "create_epic",
        title="worker-a test luke",
        repo_full_name="lkmotto/motto-mcp-server",
        body="b",
        filed_by="luke",
    )
    assert result["status"] == "active"


@requires_db
async def test_create_epic_emits_event(epics_db, server, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    _patch_httpx(
        monkeypatch,
        [],
        [_FakeResponse(201, {"number": 42, "html_url": "https://example.com/42"})],
    )

    await _call(
        server,
        "create_epic",
        title="worker-a test event",
        repo_full_name="lkmotto/motto-mcp-server",
        body="b",
    )

    async with epics_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT e.kind, e.payload
            FROM fleet.events e
            JOIN fleet.agents a ON a.id = e.agent_id
            WHERE a.name = 'motto-mcp-server' AND e.kind = 'epic.created'
            ORDER BY e.id DESC LIMIT 1
            """
        )
    assert row is not None
    assert row["kind"] == "epic.created"
    payload = row["payload"]
    if isinstance(payload, str):
        payload = _json.loads(payload)
    assert payload["issue_number"] == 42
    assert payload["repo"] == "lkmotto/motto-mcp-server"


@requires_db
async def test_create_epic_rejects_bad_repo(epics_db, server, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    with pytest.raises(Exception):
        await _call(
            server,
            "create_epic",
            title="bad",
            repo_full_name="no-slash",
            body="b",
        )


# ---------------------------------------------------------------------------
# dispatch_droid_for_epic
# ---------------------------------------------------------------------------


async def _insert_epic_row(db, *, title: str, status: str = "active") -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO public.epics
                (title, kpi_ref, rationale, plan, status,
                 gh_issue_url, gh_issue_number,
                 max_cost_usd, max_hours, success_criteria_json,
                 last_progress_at)
            VALUES ($1, $2, $3, '[]'::jsonb, $4,
                    $5, $6, $7, $8, $9::jsonb, NOW())
            RETURNING id
            """,
            title, f"kpi-{title}", "body", status,
            "https://github.com/x/y/issues/1", 1,
            25.0, 8, '["c1"]',
        )


@requires_db
async def test_dispatch_droid_spawns_session_and_locks_epic(
    epics_db, server, monkeypatch
):
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    epic_id = await _insert_epic_row(epics_db, title="worker-a test dispatch")
    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [
            _FakeResponse(201, {"sessionId": "sess_abc123", "status": "active"}),
            _FakeResponse(202, {"ok": True}),
        ],
    )

    result = await _call(
        server,
        "dispatch_droid_for_epic",
        epic_id=epic_id,
    )

    assert result["session_id"] == "sess_abc123"
    assert result["locked"] is True
    assert "computer_id" in result

    assert [c[0] for c in calls] == ["POST", "POST"]
    assert calls[0][1].endswith("/sessions")
    assert calls[1][1].endswith("/sessions/sess_abc123/messages")
    assert "worker-a test dispatch" in calls[1][2]["text"]

    async with epics_db.pool.acquire() as conn:
        sid = await conn.fetchval(
            "SELECT factory_session_id FROM public.epics WHERE id = $1", epic_id
        )
    assert sid == "sess_abc123"


@requires_db
async def test_dispatch_droid_is_idempotent_when_locked(
    epics_db, server, monkeypatch
):
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    epic_id = await _insert_epic_row(epics_db, title="worker-a test locked")
    async with epics_db.pool.acquire() as conn:
        await conn.execute(
            "UPDATE public.epics SET factory_session_id = $2 WHERE id = $1",
            epic_id, "sess_existing",
        )

    calls: list = []
    _patch_httpx(monkeypatch, calls, [])

    result = await _call(
        server,
        "dispatch_droid_for_epic",
        epic_id=epic_id,
    )
    assert result["session_id"] == "sess_existing"
    assert result["locked"] is False
    assert result["status"] == "already_locked"
    assert calls == []


@requires_db
async def test_dispatch_droid_missing_epic_errors(epics_db, server, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    _patch_httpx(monkeypatch, [], [])
    with pytest.raises(Exception):
        await _call(
            server,
            "dispatch_droid_for_epic",
            epic_id=999_999_999,
        )


@requires_db
async def test_dispatch_droid_records_event(epics_db, server, monkeypatch):
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    epic_id = await _insert_epic_row(epics_db, title="worker-a test event")
    _patch_httpx(
        monkeypatch,
        [],
        [
            _FakeResponse(201, {"sessionId": "sess_evt_1", "status": "active"}),
            _FakeResponse(202, {}),
        ],
    )

    await _call(server, "dispatch_droid_for_epic", epic_id=epic_id)

    async with epics_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT e.kind, e.payload
            FROM fleet.events e
            JOIN fleet.agents a ON a.id = e.agent_id
            WHERE a.name = 'motto-mcp-server' AND e.kind = 'epic.dispatched'
            ORDER BY e.id DESC LIMIT 1
            """
        )
    assert row is not None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = _json.loads(payload)
    assert payload["epic_id"] == epic_id
    assert payload["session_id"] == "sess_evt_1"
    assert payload["locked"] is True
