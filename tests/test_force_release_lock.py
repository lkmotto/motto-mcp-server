"""Tests for force_release_lock."""

from __future__ import annotations

import pytest

from tests.conftest import _call, insert_lock, insert_run, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_force_release_removes_active_lock(db, server):
    rid = await insert_run(db, agent_name="agent-a")
    await insert_lock(db, resource="director:cycle", holder_run=rid)

    result = await _call(server, "force_release_lock", resource="director:cycle")
    assert result == {"released": True}

    locks = await _call(server, "get_locks")
    assert all(lock["resource"] != "director:cycle" for lock in locks)


async def test_force_release_unknown_resource_is_idempotent(db, server):
    result = await _call(
        server, "force_release_lock", resource="nonexistent:resource"
    )
    assert result == {"released": False}
