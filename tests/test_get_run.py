"""Tests for get_run — full record + linked events + decisions + artifacts."""

from __future__ import annotations

import pytest

from tests.conftest import (
    _call,
    insert_artifact,
    insert_decision,
    insert_event,
    insert_run,
    requires_db,
)

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_get_run_returns_run_events_decisions_artifacts(db, server):
    rid = await insert_run(
        db, agent_name="agent-a", kind="cycle", intent="do the thing"
    )
    await insert_event(db, agent_name="agent-a", run_id=rid, kind="started")
    await insert_event(db, agent_name="agent-a", run_id=rid, kind="finished")
    await insert_decision(
        db, agent_name="agent-a", run_id=rid, choice="merge",
        rationale="ci green", payload={"pr": 42},
    )
    await insert_artifact(
        db, agent_name="agent-a", run_id=rid, kind="prompt",
        name="ideate", content={"text": "hi"},
    )

    out = await _call(server, "get_run", run_id=rid)
    assert out["run"]["id"] == rid
    assert out["run"]["kind"] == "cycle"
    assert out["run"]["intent"] == "do the thing"
    assert len(out["events"]) == 2
    assert {e["kind"] for e in out["events"]} == {"started", "finished"}
    assert len(out["decisions"]) == 1
    assert out["decisions"][0]["choice"] == "merge"
    assert out["decisions"][0]["payload"] == {"pr": 42}
    assert len(out["artifacts"]) == 1
    assert out["artifacts"][0]["name"] == "ideate"


async def test_get_run_with_no_links_returns_empty_lists(db, server):
    rid = await insert_run(db, agent_name="agent-a")
    out = await _call(server, "get_run", run_id=rid)
    assert out["run"]["id"] == rid
    assert out["events"] == []
    assert out["decisions"] == []
    assert out["artifacts"] == []
