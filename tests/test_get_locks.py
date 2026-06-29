"""Tests for get_locks — only active (non-expired) locks should be returned."""

from __future__ import annotations

import pytest

from tests.conftest import _call, insert_lock, insert_run, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_get_locks_returns_active_excludes_expired(db, server):
    rid = await insert_run(db, agent_name="agent-a")
    await insert_lock(db, resource="director:cycle", holder_run=rid)
    await insert_lock(db, resource="stale:resource", holder_run=rid, expires_in_seconds=-60)

    out = await _call(server, "get_locks")
    resources = {lock["resource"] for lock in out}
    assert "director:cycle" in resources
    assert "stale:resource" not in resources
    held = next(lock for lock in out if lock["resource"] == "director:cycle")
    assert held["holder_run"] == rid
    assert held["expires_at"] is not None


async def test_get_locks_empty_returns_empty(db, server):
    assert await _call(server, "get_locks") == []
