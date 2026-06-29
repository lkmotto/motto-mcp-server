"""Tests for replay_run — parent_run + child_runs hierarchy."""

from __future__ import annotations

import pytest

from tests.conftest import (
    _call,
    insert_decision,
    insert_event,
    insert_run,
    requires_db,
)

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_replay_run_includes_parent_and_children(db, server):
    parent_id = await insert_run(db, agent_name="agent-a", kind="parent")
    child_id_1 = await insert_run(db, agent_name="agent-a", kind="child-1", parent_run_id=parent_id)
    child_id_2 = await insert_run(db, agent_name="agent-a", kind="child-2", parent_run_id=parent_id)

    await insert_event(db, agent_name="agent-a", run_id=parent_id, kind="ev")
    await insert_decision(db, agent_name="agent-a", run_id=parent_id, choice="dispatch")

    out = await _call(server, "replay_run", run_id=parent_id)

    assert out["run"]["id"] == parent_id
    assert out["parent_run"] is None
    child_ids = {c["id"] for c in out["child_runs"]}
    assert child_ids == {child_id_1, child_id_2}
    assert len(out["events"]) == 1
    assert len(out["decisions"]) == 1


async def test_replay_child_run_links_back_to_parent(db, server):
    parent_id = await insert_run(db, agent_name="agent-a", kind="parent")
    child_id = await insert_run(db, agent_name="agent-a", kind="child", parent_run_id=parent_id)

    out = await _call(server, "replay_run", run_id=child_id)
    assert out["run"]["id"] == child_id
    assert out["parent_run"] is not None
    assert out["parent_run"]["id"] == parent_id
    assert out["child_runs"] == []


async def test_replay_run_with_no_links(db, server):
    rid = await insert_run(db, agent_name="agent-a")
    out = await _call(server, "replay_run", run_id=rid)
    assert out["run"]["id"] == rid
    assert out["parent_run"] is None
    assert out["child_runs"] == []
    assert out["events"] == []
    assert out["decisions"] == []
    assert out["artifacts"] == []
