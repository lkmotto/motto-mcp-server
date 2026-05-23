"""Tests for epic_status / pause_epic / kill_epic MCP tools (Worker B).

Same scripted-httpx pattern as test_epics_worker_a.py. The public.epics
table is owned by motto-director migration 0006_epics.sql, so the tests
skip when that table is not present in the target DB.
"""

from __future__ import annotations

import json as _json
from typing import Any

import pytest

from tests.conftest import _call, insert_agent, requires_db

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
            "DELETE FROM public.epics WHERE title LIKE 'worker-b test%'"
        )


@pytest.fixture
async def epics_db(db):
    if not await _epics_table_exists(db):
        pytest.skip("public.epics not present in test DB")
    await _wipe(db)
    yield db
    await _wipe(db)


# ── scripted httpx helpers ───────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status: int, body: Any = None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = str(self._body)

    def json(self) -> Any:
        return self._body


class _FakeAsyncClient:
    """Records GET / POST calls and returns scripted responses in order.

    Calls are appended as (method, url, json_body) for assertions.
    """

    def __init__(self, calls: list, responses: list):
        self._calls = calls
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def post(self, url, headers=None, json=None, **_):
        self._calls.append(("POST", url, json or {}))
        if not self._responses:
            raise AssertionError(f"unexpected POST {url}")
        return self._responses.pop(0)

    async def get(self, url, headers=None, params=None, **_):
        self._calls.append(("GET", url, params or {}))
        if not self._responses:
            raise AssertionError(f"unexpected GET {url}")
        return self._responses.pop(0)


def _patch_httpx(monkeypatch, calls: list, responses: list) -> None:
    from mcp_server.tools import epics as epics_mod

    def _ctor(*_a, **_kw):
        return _FakeAsyncClient(calls, responses)

    monkeypatch.setattr(epics_mod.httpx, "AsyncClient", _ctor)


# ── epic-row insertion helper ─────────────────────────────────────────────────


async def _insert_epic(
    db,
    *,
    title: str,
    status: str = "active",
    issue_number: int = 1,
    issue_url: str | None = None,
    factory_session_id: str | None = None,
    cost_so_far_usd: float = 0.0,
    success_criteria: list[str] | None = None,
) -> int:
    url = issue_url or f"https://github.com/lkmotto/motto-mcp-server/issues/{issue_number}"
    sc_json = _json.dumps(success_criteria or ["c1"])
    async with db.pool.acquire() as conn:
        eid = await conn.fetchval(
            """
            INSERT INTO public.epics
                (title, kpi_ref, rationale, plan, status,
                 gh_issue_url, gh_issue_number, factory_session_id,
                 cost_so_far_usd, max_cost_usd, max_hours,
                 success_criteria_json, last_progress_at)
            VALUES ($1, $2, $3, '[]'::jsonb, $4,
                    $5, $6, $7, $8, $9, $10, $11::jsonb, NOW())
            RETURNING id
            """,
            title, f"kpi-{title}", "body text", status,
            url, int(issue_number), factory_session_id,
            float(cost_so_far_usd), 25.0, 8, sc_json,
        )
        return int(eid)


# ── epic_status ─────────────────────────────────────────────────────────────


@requires_db
async def test_epic_status_aggregates_gh_and_factory(epics_db, server, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    epic_id = await _insert_epic(
        epics_db,
        title="worker-b test status",
        issue_number=4242,
        factory_session_id="sess_xyz",
        cost_so_far_usd=1.25,
        success_criteria=["a", "b"],
    )

    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [
            _FakeResponse(200, {"body": "issue body from GH"}),
            _FakeResponse(
                200,
                [
                    {
                        "id": 1,
                        "user": {"login": "luke"},
                        "created_at": "2026-05-22T01:00:00Z",
                        "body": "first comment",
                    },
                    {
                        "id": 2,
                        "user": {"login": "droid"},
                        "created_at": "2026-05-22T02:00:00Z",
                        "body": "second comment",
                    },
                ],
            ),
            _FakeResponse(200, {"status": "running", "id": "sess_xyz"}),
        ],
    )

    out = await _call(server, "epic_status", epic_id=epic_id)

    assert out["epic"]["id"] == epic_id
    assert out["epic"]["status"] == "active"
    assert out["gh"]["repo"] == "lkmotto/motto-mcp-server"
    assert out["gh"]["issue_number"] == 4242
    assert out["gh"]["body"] == "issue body from GH"
    assert len(out["gh"]["comments"]) == 2
    assert out["gh"]["comments"][0]["user"] == "luke"
    assert out["factory_session"]["status"] == "running"
    assert out["factory_session"]["session_id"] == "sess_xyz"
    assert out["cost_so_far_usd"] == 1.25
    assert out["success_criteria_json"] == ["a", "b"]
    # epic with no run_id has no fleet events
    assert out["fleet_events"] == []

    # 3 HTTP calls: issue body, comments, factory session
    assert len(calls) == 3
    assert calls[0][0] == "GET"
    assert "/issues/4242" in calls[0][1]
    assert calls[1][0] == "GET"
    assert "/comments" in calls[1][1]
    assert calls[2][0] == "GET"
    assert "/sessions/sess_xyz" in calls[2][1]


@requires_db
async def test_epic_status_no_factory_session_skips_factory_call(
    epics_db, server, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    epic_id = await _insert_epic(
        epics_db, title="worker-b test no session", issue_number=11
    )

    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [
            _FakeResponse(200, {"body": "b"}),
            _FakeResponse(200, []),
        ],
    )

    out = await _call(server, "epic_status", epic_id=epic_id)
    assert out["factory_session"] is None
    assert len(calls) == 2  # no Factory call when no session id


@requires_db
async def test_epic_status_missing_epic_raises(epics_db, server, monkeypatch):
    _patch_httpx(monkeypatch, [], [])
    with pytest.raises((ValueError, RuntimeError, Exception)):  # noqa: B017
        await _call(server, "epic_status", epic_id=999_999_999)


# ── pause_epic ─────────────────────────────────────────────────────────────


@requires_db
async def test_pause_epic_updates_status_and_posts_comment(
    epics_db, server, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    epic_id = await _insert_epic(
        epics_db,
        title="worker-b test pause",
        issue_number=7000,
        factory_session_id="sess_pause",
    )

    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [
            _FakeResponse(202, {"ok": True}),  # factory interrupt
            _FakeResponse(201, {"id": 1}),  # gh issue comment
        ],
    )

    result = await _call(
        server, "pause_epic", epic_id=epic_id, reason="cost spike"
    )

    assert result["epic_id"] == epic_id
    assert result["status"] == "paused"
    assert result["factory_interrupt"]["ok"] is True
    assert result["comment_posted"] is True
    assert result["reason"] == "cost spike"

    # DB row paused
    async with epics_db.pool.acquire() as conn:
        s = await conn.fetchval(
            "SELECT status FROM public.epics WHERE id = $1", epic_id
        )
    assert s == "paused"

    # 2 HTTP calls: factory interrupt + gh comment
    assert [c[0] for c in calls] == ["POST", "POST"]
    assert "/sessions/sess_pause/interrupt" in calls[0][1]
    assert "/issues/7000/comments" in calls[1][1]
    assert "cost spike" in calls[1][2]["body"]


@requires_db
async def test_pause_epic_no_session_skips_interrupt(
    epics_db, server, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    epic_id = await _insert_epic(
        epics_db, title="worker-b test pause solo", issue_number=7001
    )

    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [_FakeResponse(201, {"id": 1})],  # only gh comment
    )

    result = await _call(server, "pause_epic", epic_id=epic_id, reason="r")
    assert result["status"] == "paused"
    assert result["factory_interrupt"]["ok"] is False
    assert result["comment_posted"] is True
    assert len(calls) == 1
    assert "/issues/7001/comments" in calls[0][1]


@requires_db
async def test_pause_epic_records_event(epics_db, server, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    await insert_agent(epics_db, "motto-mcp-server")
    epic_id = await _insert_epic(
        epics_db, title="worker-b test pause event", issue_number=7002
    )
    _patch_httpx(monkeypatch, [], [_FakeResponse(201, {"id": 1})])

    await _call(server, "pause_epic", epic_id=epic_id, reason="why")

    async with epics_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT e.kind, e.payload
            FROM fleet.events e
            JOIN fleet.agents a ON a.id = e.agent_id
            WHERE a.name = 'motto-mcp-server' AND e.kind = 'epic.paused'
            ORDER BY e.id DESC LIMIT 1
            """
        )
    assert row is not None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = _json.loads(payload)
    assert payload["epic_id"] == epic_id
    assert payload["status"] == "paused"
    assert payload["reason"] == "why"


@requires_db
async def test_pause_epic_missing_epic_raises(epics_db, server, monkeypatch):
    _patch_httpx(monkeypatch, [], [])
    with pytest.raises((ValueError, RuntimeError, Exception)):  # noqa: B017
        await _call(server, "pause_epic", epic_id=999_999_999, reason="x")


# ── kill_epic ───────────────────────────────────────────────────────────────


@requires_db
async def test_kill_epic_abandons_and_drafts_prs(
    epics_db, server, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("FACTORY_API_KEY", "fa_test")
    epic_id = await _insert_epic(
        epics_db,
        title="worker-b test kill",
        issue_number=8000,
        factory_session_id="sess_kill",
    )

    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [
            # factory interrupt
            _FakeResponse(202, {}),
            # search PRs
            _FakeResponse(
                200,
                {
                    "items": [
                        {
                            "number": 31,
                            "node_id": "PR_node_31",
                            "html_url": "https://github.com/x/y/pull/31",
                            "title": "fix(epic): something",
                            "draft": False,
                        },
                        {
                            "number": 32,
                            "node_id": "PR_node_32",
                            "html_url": "https://github.com/x/y/pull/32",
                            "title": "feat(epic): other",
                            "draft": False,
                        },
                    ]
                },
            ),
            # graphql draft PR 31
            _FakeResponse(
                200,
                {"data": {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}},
            ),
            # graphql draft PR 32
            _FakeResponse(
                200,
                {"data": {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}},
            ),
            # post issue comment
            _FakeResponse(201, {"id": 99}),
        ],
    )

    result = await _call(
        server, "kill_epic", epic_id=epic_id, reason="budget blown"
    )

    assert result["epic_id"] == epic_id
    assert result["status"] == "abandoned"
    assert result["factory_interrupt"]["ok"] is True
    assert len(result["drafted_prs"]) == 2
    assert all(p["drafted"] for p in result["drafted_prs"])
    assert result["comment_posted"] is True

    # DB row abandoned + closed_reason stamped
    async with epics_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, closed_reason, closed_at FROM public.epics WHERE id = $1",
            epic_id,
        )
    assert row["status"] == "abandoned"
    assert row["closed_reason"] == "budget blown"
    assert row["closed_at"] is not None


@requires_db
async def test_kill_epic_no_open_prs_still_succeeds(
    epics_db, server, monkeypatch
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    epic_id = await _insert_epic(
        epics_db, title="worker-b test kill empty", issue_number=8001
    )

    calls: list = []
    _patch_httpx(
        monkeypatch,
        calls,
        [
            _FakeResponse(200, {"items": []}),  # search PRs
            _FakeResponse(201, {"id": 1}),  # gh comment
        ],
    )

    result = await _call(server, "kill_epic", epic_id=epic_id, reason="r")
    assert result["status"] == "abandoned"
    assert result["drafted_prs"] == []
    assert result["comment_posted"] is True


@requires_db
async def test_kill_epic_records_event(epics_db, server, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    await insert_agent(epics_db, "motto-mcp-server")
    epic_id = await _insert_epic(
        epics_db, title="worker-b test kill event", issue_number=8002
    )
    _patch_httpx(
        monkeypatch,
        [],
        [
            _FakeResponse(200, {"items": []}),
            _FakeResponse(201, {"id": 1}),
        ],
    )

    await _call(server, "kill_epic", epic_id=epic_id, reason="done")

    async with epics_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT e.kind, e.payload
            FROM fleet.events e
            JOIN fleet.agents a ON a.id = e.agent_id
            WHERE a.name = 'motto-mcp-server' AND e.kind = 'epic.killed'
            ORDER BY e.id DESC LIMIT 1
            """
        )
    assert row is not None
    payload = row["payload"]
    if isinstance(payload, str):
        payload = _json.loads(payload)
    assert payload["epic_id"] == epic_id
    assert payload["status"] == "abandoned"
    assert payload["reason"] == "done"


@requires_db
async def test_kill_epic_missing_epic_raises(epics_db, server, monkeypatch):
    _patch_httpx(monkeypatch, [], [])
    with pytest.raises((ValueError, RuntimeError, Exception)):  # noqa: B017
        await _call(server, "kill_epic", epic_id=999_999_999)
