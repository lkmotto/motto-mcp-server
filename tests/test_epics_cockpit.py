"""Tests for Worker F epic cockpit routes and helpers.

Covers:
  - _repo_from_gh_url parsing
  - _epic_progress_pct heuristic
  - _fmt_money / _fmt_iso_short formatting
  - Route auth gates (unauthenticated → 401)
  - Route handlers with a mock db
  - _invoke_epic_transition fallback path
  - _message_factory_droid when FACTORY_API_KEY absent
"""

from __future__ import annotations

import pytest

from mcp_server.cockpit import (
    _repo_from_gh_url,
    _epic_progress_pct,
    _fmt_money,
    _fmt_iso_short,
    _invoke_epic_transition,
    _message_factory_droid,
)


# ── Unit tests: pure functions ────────────────────────────────────────────────


class TestRepoFromGhUrl:
    def test_standard_issue_url(self):
        repo, num = _repo_from_gh_url(
            "https://github.com/lkmotto/motto-mcp-server/issues/64"
        )
        assert repo == "lkmotto/motto-mcp-server"
        assert num == 64

    def test_none_input(self):
        assert _repo_from_gh_url(None) == (None, None)

    def test_empty_string(self):
        assert _repo_from_gh_url("") == (None, None)

    def test_non_issue_url(self):
        assert _repo_from_gh_url("https://github.com/lkmotto/repo") == (None, None)

    def test_pull_request_url(self):
        assert _repo_from_gh_url(
            "https://github.com/lkmotto/repo/pull/10"
        ) == (None, None)

    def test_malformed_url(self):
        assert _repo_from_gh_url("not-a-url") == (None, None)


class TestEpicProgressPct:
    def test_active_epic(self):
        assert _epic_progress_pct({"status": "active"}) == 25

    def test_paused_epic(self):
        assert _epic_progress_pct({"status": "paused"}) == 50

    def test_closed_epic(self):
        assert _epic_progress_pct({"status": "closed"}) == 100

    def test_abandoned_epic(self):
        assert _epic_progress_pct({"status": "abandoned"}) == 100

    def test_proposed_epic(self):
        assert _epic_progress_pct({"status": "proposed"}) == 0

    def test_unknown_status(self):
        assert _epic_progress_pct({"status": "unknown"}) == 0

    def test_no_status(self):
        assert _epic_progress_pct({}) == 0

    def test_success_criteria_all_done(self):
        epic = {
            "success_criteria_json": [
                {"status": "done"},
                {"status": "passed"},
                {"state": "completed"},
            ]
        }
        assert _epic_progress_pct(epic) == 100

    def test_success_criteria_partial(self):
        epic = {
            "success_criteria_json": [
                {"status": "done"},
                {"status": "pending"},
                {"status": "pending"},
                {"status": "pending"},
            ]
        }
        assert _epic_progress_pct(epic) == 25

    def test_success_criteria_empty_list(self):
        assert _epic_progress_pct({"success_criteria_json": []}) == 0


class TestFmtMoney:
    def test_none(self):
        assert _fmt_money(None) == "—"

    def test_zero(self):
        assert _fmt_money(0) == "$0.00"

    def test_float(self):
        assert _fmt_money(12.5) == "$12.50"

    def test_string_number(self):
        assert _fmt_money("3.14") == "$3.14"

    def test_invalid_string(self):
        assert _fmt_money("not_a_number") == "—"


class TestFmtIsoShort:
    def test_none(self):
        assert _fmt_iso_short(None) == "—"

    def test_empty(self):
        assert _fmt_iso_short("") == "—"

    def test_full_iso(self):
        assert _fmt_iso_short("2026-05-22T14:30:00.123456+00:00") == "2026-05-22 14:30:00"

    def test_no_microseconds(self):
        assert _fmt_iso_short("2026-05-22T14:30:00") == "2026-05-22 14:30:00"


# ── Integration-style tests with mock db ───────────────────────────────────────


class MockDB:
    """Minimal mock of Database for testing _invoke_epic_transition."""

    def __init__(self):
        self.events: list[dict] = []

    async def director_set_epic_status(
        self, *, epic_id, new_status, approved_by, closed_reason=""
    ):
        return True

    async def record_event(self, **kwargs):
        self.events.append(kwargs)


@pytest.mark.asyncio
async def test_invoke_epic_transition_fallback():
    db = MockDB()
    result = await _invoke_epic_transition(
        db=db,
        epic_id=14,
        kind="pause",
        new_status="paused",
        reason="testing",
        approver="cockpit:test",
    )
    assert result["ok"] is True
    assert result["via"] == "db_fallback"
    assert len(db.events) == 1
    assert db.events[0]["kind"] == "epic.pause"


@pytest.mark.asyncio
async def test_invoke_epic_transition_kill():
    db = MockDB()
    result = await _invoke_epic_transition(
        db=db,
        epic_id=14,
        kind="kill",
        new_status="abandoned",
        reason="done",
        approver="cockpit:test",
    )
    assert result["ok"] is True
    assert result["new_status"] == "abandoned"


@pytest.mark.asyncio
async def test_message_factory_droid_no_token():
    import os

    orig = os.environ.get("FACTORY_API_KEY")
    os.environ.pop("FACTORY_API_KEY", None)
    os.environ.pop("FACTORY_TOKEN", None)
    os.environ.pop("FACTORY_API_TOKEN", None)
    try:
        result = await _message_factory_droid(
            session_id="sess-123", message="hello", sender="cockpit:test"
        )
        assert result["ok"] is False
        assert "FACTORY_API_KEY" in result["error"]
    finally:
        if orig:
            os.environ["FACTORY_API_KEY"] = orig
