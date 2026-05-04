"""Tests for list_runs."""

from __future__ import annotations

import pytest

from tests.conftest import _call, insert_run, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_list_runs_returns_newest_first_and_paginates(db, server):
    for i in range(5):
        await insert_run(db, agent_name="agent-a", kind=f"k{i}")
    out = await _call(server, "list_runs", limit=3)
    assert len(out) == 3
    # Newest first — kinds were inserted k0..k4, so newest is k4.
    assert out[0]["kind"] == "k4"
    assert out[2]["kind"] == "k2"


async def test_list_runs_filters_by_status(db, server):
    await insert_run(db, agent_name="agent-a", status="success")
    await insert_run(db, agent_name="agent-a", status="error")
    await insert_run(db, agent_name="agent-a", status="success")
    out = await _call(server, "list_runs", status="success")
    assert len(out) == 2
    assert all(r["status"] == "success" for r in out)


async def test_list_runs_filters_by_agent(db, server):
    await insert_run(db, agent_name="agent-a")
    await insert_run(db, agent_name="agent-b")
    await insert_run(db, agent_name="agent-b")
    out = await _call(server, "list_runs", agent_name="agent-b")
    assert len(out) == 2
    assert all(r["agent_name"] == "agent-b" for r in out)


async def test_list_runs_empty_db_returns_empty_list(db, server):
    out = await _call(server, "list_runs")
    assert out == []
