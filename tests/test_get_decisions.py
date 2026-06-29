"""Tests for get_decisions filter combinations."""

from __future__ import annotations

import pytest

from tests.conftest import _call, insert_decision, insert_run, requires_db

pytestmark = [requires_db, pytest.mark.asyncio]


async def test_get_decisions_filters_by_run_agent_choice(db, server):
    r1 = await insert_run(db, agent_name="agent-a")
    r2 = await insert_run(db, agent_name="agent-b")

    await insert_decision(db, agent_name="agent-a", run_id=r1, choice="merge")
    await insert_decision(db, agent_name="agent-a", run_id=r1, choice="skip")
    await insert_decision(db, agent_name="agent-b", run_id=r2, choice="merge")
    await insert_decision(db, agent_name="agent-b", run_id=None, choice="merge")

    # No filter — all four.
    assert len(await _call(server, "get_decisions")) == 4

    # By run_id.
    by_run = await _call(server, "get_decisions", run_id=r1)
    assert len(by_run) == 2
    assert all(d["run_id"] == r1 for d in by_run)

    # By agent.
    by_agent = await _call(server, "get_decisions", agent_name="agent-b")
    assert len(by_agent) == 2

    # By choice.
    by_choice = await _call(server, "get_decisions", choice="merge")
    assert len(by_choice) == 3

    # Combined: run + choice.
    combined = await _call(server, "get_decisions", run_id=r1, choice="merge")
    assert len(combined) == 1
    assert combined[0]["choice"] == "merge"
    assert combined[0]["run_id"] == r1


async def test_get_decisions_empty_db_returns_empty(db, server):
    assert await _call(server, "get_decisions") == []
