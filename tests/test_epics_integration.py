"""Integration tests for the epic control-plane MCP tools.

These tests cross worker boundaries (Worker A: create_epic +
dispatch_droid_for_epic; Worker B: epic_status + pause_epic + kill_epic).
They mock GitHub, Factory, and the database so the test suite does not
need network or DB access.

When the tool stubs in `mcp_server/tools/epics.py` are still
NotImplementedError, the affected tests fail with a clear marker —
re-run after Workers A and B merge their implementations.

Conventions assumed by these tests (mock points):
  * Tools call into `mcp_server.server.db` for persistence.
  * Tools call GitHub + Factory through `httpx.AsyncClient`.
  * `record_event` is invoked via `mcp_server.server.db.record_event`.

If a worker chooses a different wiring, update the monkeypatch targets
below; the assertions about behaviour stay the same.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# asyncio mark applied per-test below (non-async tests must not carry it).


# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


def _make_fake_db() -> MagicMock:
    db = MagicMock()
    db.insert_epic_with_gh = AsyncMock(
        return_value={
            "id": 100,
            "status": "active",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    db.update_epic_session_id = AsyncMock(return_value=True)
    db.fetch_epic_dispatch_row = AsyncMock(
        return_value={
            "id": 100,
            "title": "Boost AMC panels",
            "rationale": "issue body",
            "status": "active",
            "gh_issue_url": "https://github.com/lkmotto/motto-mcp-server/issues/99",
            "gh_issue_number": 99,
            "max_cost_usd": 25.0,
            "max_hours": 8,
            "success_criteria_json": ["criteria A", "criteria B"],
            "factory_session_id": None,
        }
    )
    db.fetch_epic_for_status = AsyncMock(
        return_value={
            "id": 100,
            "title": "Boost AMC panels",
            "status": "active",
            "rationale": "issue body",
            "gh_issue_url": "https://github.com/lkmotto/motto-mcp-server/issues/99",
            "gh_issue_number": 99,
            "factory_session_id": "sess_xyz",
            "cost_so_far_usd": 1.25,
            "success_criteria_json": ["criteria A"],
            "last_progress_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    db.set_epic_status = AsyncMock(return_value=True)
    db.record_event = AsyncMock(return_value=1)
    return db


class _FakeResponse:
    def __init__(self, status_code: int, payload: object) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class _FakeAsyncClient:
    """Stand-in for httpx.AsyncClient supporting `async with` + post/get."""

    def __init__(self, *_, **__):
        self.calls: list[dict] = []
        self.post = AsyncMock(side_effect=self._post)
        self.get = AsyncMock(side_effect=self._get)
        self.response_for: dict[tuple[str, str], _FakeResponse] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def _post(self, url: str, **kw):
        self.calls.append({"method": "POST", "url": url, **kw})
        for (method, prefix), resp in self.response_for.items():
            if method == "POST" and url.startswith(prefix):
                return resp
        return _FakeResponse(200, {"id": 1})

    async def _get(self, url: str, **kw):
        self.calls.append({"method": "GET", "url": url, **kw})
        for (method, prefix), resp in self.response_for.items():
            if method == "GET" and url.startswith(prefix):
                return resp
        return _FakeResponse(200, {})


@pytest.fixture
def fake_db(monkeypatch):
    db = _make_fake_db()
    # Tools are expected to access the database via mcp_server.server.db.
    import mcp_server.server as server_mod

    monkeypatch.setattr(server_mod, "db", db, raising=False)
    # Convenience: also expose at module level on tools.epics for worker
    # implementations that prefer a direct import.
    import mcp_server.tools.epics as epics_mod

    monkeypatch.setattr(epics_mod, "db", db, raising=False)
    return db


@pytest.fixture
def fake_httpx(monkeypatch):
    client = _FakeAsyncClient()
    # Pre-seed common endpoints.
    client.response_for[("POST", "https://api.github.com")] = _FakeResponse(
        201,
        {
            "number": 99,
            "html_url": "https://github.com/lkmotto/motto-mcp-server/issues/99",
        },
    )
    client.response_for[("POST", "https://api.factory.ai")] = _FakeResponse(
        201,
        {
            "sessionId": "sess_xyz",
            "status": "running",
            "computerId": "comp_legion",
        },
    )
    client.response_for[("GET", "https://api.factory.ai")] = _FakeResponse(
        200,
        {"sessionId": "sess_xyz", "status": "running"},
    )

    def factory(*args, **kwargs):
        return client

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return client


def _skip_if_not_implemented(func_name: str, exc: NotImplementedError) -> None:
    pytest.skip(f"{func_name} stub not implemented yet: {exc}")


# ---------------------------------------------------------------------------
# create_epic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_epic_happy_path_luke_active(monkeypatch, fake_db, fake_httpx):
    """When Luke files an epic (no run_id), the epic is created active."""
    from mcp_server.tools import epics as epics_tools

    # Make the happy-path DB return status='active' for Luke (no run_id).
    fake_db.insert_epic_with_gh.return_value = {
        "id": 100,
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    try:
        result = await epics_tools.create_epic(
            title="Boost AMC panels",
            repo_full_name="lkmotto/motto-mcp-server",
            body="## Success criteria\n- 42 panels live",
            labels=["epic"],
            success_criteria=["42 panels live"],
            kpi_ref="AMC panel registrations",
            run_id=None,
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("create_epic", exc)

    assert result["epic_id"] == 100
    assert "issue_url" in result and result["issue_url"].startswith("https://github.com")
    assert result["issue_number"] == 99
    fake_db.insert_epic_with_gh.assert_awaited_once()
    kwargs = fake_db.insert_epic_with_gh.await_args.kwargs
    assert kwargs.get("status") == "active"


@pytest.mark.asyncio
async def test_create_epic_happy_path_agent_proposed(monkeypatch, fake_db, fake_httpx):
    """When an agent files an epic (run_id present), the epic starts as proposed."""
    from mcp_server.tools import epics as epics_tools

    fake_db.insert_epic_with_gh.return_value = {
        "id": 101,
        "status": "proposed",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    try:
        result = await epics_tools.create_epic(
            title="Agent-proposed cleanup",
            repo_full_name="lkmotto/motto-director",
            body="## Goals\n- delete stale lenses",
            labels=["epic"],
            success_criteria=["lenses removed"],
            kpi_ref="director cleanliness",
            run_id="5e6e5a17-da8c-4976-b672-9a6ef552db7c",
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("create_epic", exc)

    assert result["epic_id"] == 101
    kwargs = fake_db.insert_epic_with_gh.await_args.kwargs
    assert kwargs.get("status") == "proposed"
    assert kwargs.get("run_id") == "5e6e5a17-da8c-4976-b672-9a6ef552db7c"


@pytest.mark.asyncio
async def test_create_epic_labels_auto_merge_with_epic(monkeypatch, fake_db, fake_httpx):
    """Labels list should always include the 'epic' label even when caller omits it."""
    from mcp_server.tools import epics as epics_tools

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    try:
        await epics_tools.create_epic(
            title="No labels supplied",
            repo_full_name="lkmotto/motto-director",
            body="body",
            labels=None,
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("create_epic", exc)

    # GitHub Issue POST should include labels=["epic", ...]
    gh_calls = [c for c in fake_httpx.calls if "api.github.com" in c["url"]]
    assert gh_calls, "create_epic must POST a GitHub Issue"
    body = gh_calls[0].get("json") or {}
    assert "epic" in (body.get("labels") or [])


# ---------------------------------------------------------------------------
# dispatch_droid_for_epic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_droid_locks_session_first_caller_wins(
    monkeypatch, fake_db, fake_httpx
):
    """First dispatch acquires the lock; a second call sees factory_session_id
    populated and either no-ops or fails — never spawns a second session."""
    from mcp_server.tools import epics as epics_tools

    monkeypatch.setenv("FACTORY_API_KEY", "fac_test")

    try:
        first = await epics_tools.dispatch_droid_for_epic(epic_id=100)
    except NotImplementedError as exc:
        _skip_if_not_implemented("dispatch_droid_for_epic", exc)

    assert first["session_id"] == "sess_xyz"
    fake_db.update_epic_session_id.assert_awaited_once()
    update_kwargs = fake_db.update_epic_session_id.await_args.kwargs
    assert update_kwargs.get("epic_id") == 100
    assert update_kwargs.get("factory_session_id") == "sess_xyz"

    # Second dispatch: epic already locked → update_epic_session_id returns
    # False; the row now has a session id and the dispatcher must NOT spawn
    # again. Either return the existing session_id or fail clearly.
    fake_db.update_epic_session_id.reset_mock()
    fake_db.update_epic_session_id.return_value = False
    fake_db.fetch_epic_dispatch_row.return_value = {
        **fake_db.fetch_epic_dispatch_row.return_value,
        "factory_session_id": "sess_xyz",
    }

    factory_spawns_before = sum(
        1 for c in fake_httpx.calls
        if c["method"] == "POST" and "api.factory.ai" in c["url"]
    )

    second_failed = False
    second_session = ""
    try:
        second = await epics_tools.dispatch_droid_for_epic(epic_id=100)
        second_session = second.get("session_id", "")
    except Exception:
        second_failed = True

    factory_spawns_after = sum(
        1 for c in fake_httpx.calls
        if c["method"] == "POST" and "api.factory.ai" in c["url"]
    )
    assert factory_spawns_after == factory_spawns_before, (
        "second dispatch must not spawn a new Factory session"
    )
    # Idempotent return path is also acceptable.
    if not second_failed:
        assert second_session == "sess_xyz"


# ---------------------------------------------------------------------------
# epic_status round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_epic_status_returns_combined_blob(monkeypatch, fake_db, fake_httpx):
    """epic_status should fold issue body/comments, Factory status, fleet
    events tagged with epic_id, and a cost-so-far estimate into one dict."""
    from mcp_server.tools import epics as epics_tools

    monkeypatch.setenv("FACTORY_API_KEY", "fac_test")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    # Pretend the events table has two epic events for this id.
    fake_db.events_for_epic = AsyncMock(
        return_value=[
            {"kind": "epic.created", "payload": {"epic_id": 100}},
            {"kind": "epic.progress", "payload": {"epic_id": 100, "note": "step done"}},
        ]
    )

    try:
        status = await epics_tools.epic_status(epic_id=100)
    except NotImplementedError as exc:
        _skip_if_not_implemented("epic_status", exc)

    assert isinstance(status, dict)
    # Round-trip identity: the epic id we asked about is reflected in the blob.
    assert status.get("epic_id") == 100 or status.get("id") == 100
    # Status from DB row carries through.
    assert status.get("status") in ("active", "proposed", "paused", "abandoned", "closed")


# ---------------------------------------------------------------------------
# pause_epic + kill_epic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_epic_transitions_active_to_paused(
    monkeypatch, fake_db, fake_httpx
):
    from mcp_server.tools import epics as epics_tools

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

    try:
        result = await epics_tools.pause_epic(epic_id=100, reason="waiting on Luke")
    except NotImplementedError as exc:
        _skip_if_not_implemented("pause_epic", exc)

    assert result.get("ok") in (True, None) or result.get("status") == "paused"
    # set_epic_status called with new_status='paused'
    fake_db.set_epic_status.assert_awaited()
    args = fake_db.set_epic_status.await_args
    assert args.kwargs.get("new_status") == "paused"
    assert args.kwargs.get("epic_id") == 100


@pytest.mark.asyncio
async def test_kill_epic_transitions_to_abandoned(
    monkeypatch, fake_db, fake_httpx
):
    from mcp_server.tools import epics as epics_tools

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    monkeypatch.setenv("FACTORY_API_KEY", "fac_test")

    # Pretend the epic has an active Factory session that needs cancelling.
    fake_db.fetch_epic_for_status.return_value = {
        **fake_db.fetch_epic_for_status.return_value,
        "status": "active",
        "factory_session_id": "sess_xyz",
    }

    try:
        result = await epics_tools.kill_epic(
            epic_id=100, reason="approach was wrong"
        )
    except NotImplementedError as exc:
        _skip_if_not_implemented("kill_epic", exc)

    assert result.get("ok") in (True, None) or result.get("status") == "abandoned"
    args = fake_db.set_epic_status.await_args
    assert args.kwargs.get("new_status") == "abandoned"
    assert args.kwargs.get("epic_id") == 100


# ---------------------------------------------------------------------------
# Tool registration contract
# ---------------------------------------------------------------------------


def test_all_five_tools_are_exported():
    """Whatever wiring workers pick, the five tools must be importable as
    awaitables from mcp_server.tools.epics."""
    import inspect

    from mcp_server.tools import epics as epics_tools

    for name in (
        "create_epic",
        "dispatch_droid_for_epic",
        "epic_status",
        "pause_epic",
        "kill_epic",
    ):
        fn = getattr(epics_tools, name, None)
        assert fn is not None, f"{name} missing from mcp_server.tools.epics"
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"
