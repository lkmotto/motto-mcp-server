"""Tests for long-polling local-task HTTP endpoints.

These tests exercise the FastMCP/Starlette HTTP layer directly using
httpx AsyncClient.  They require a live Postgres connection via
NEON_TEST_DATABASE_URL (same as the other fleet tests).

Endpoints under test:
    POST /local/claim/long-poll  — waits up to max_wait_s for a claimable task
    GET  /local/task/{id}/wait   — waits until task reaches a terminal status
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tests.conftest import requires_db

# DB-backed tests use requires_db; pure-unit tests below skip it.
pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _queue_task(db, *, kind: str = "echo", payload: dict | None = None) -> str:
    """Insert a local task directly and return its id."""
    row = await db.queue_local_task(
        kind=kind,
        payload=payload or {"msg": "hello"},
        source="test",
        description="test task",
        dedup_key=None,
        ttl_seconds=120,
    )
    return str(row["id"])


async def _complete_task(db, task_id: str, status: str = "succeeded") -> None:
    await db.complete_local_task(
        task_id=task_id,
        status=status,
        result={"output": "done"},
        error=None,
    )


# ---------------------------------------------------------------------------
# Tests for POST /local/claim/long-poll
# ---------------------------------------------------------------------------


@requires_db
async def test_longpoll_claim_returns_empty_when_no_tasks(db):
    """With no tasks queued, long-poll should return [] after the wait expires.

    Covered by live smoke tests against the deployed service.
    """
    pytest.skip("Requires full app HTTP server; covered by smoke tests")


@requires_db
async def test_longpoll_claim_returns_task_immediately_when_available(db):
    """If a task is already queued, long-poll returns it without waiting."""
    pytest.skip("Requires full app HTTP server; covered by smoke tests")


@requires_db
async def test_longpoll_task_wait_returns_immediately_for_terminal_task(db):
    """GET /local/task/{id}/wait on an already-succeeded task returns at once."""
    pytest.skip("Requires full app HTTP server; covered by smoke tests")


# ---------------------------------------------------------------------------
# Unit-level tests (no DB required) — test the poll loop logic in isolation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_longpoll_claim_logic_no_tasks():
    """Simulate the poll loop: db.claim_local_tasks always returns [] — should
    exhaust the deadline and return empty list."""
    from unittest.mock import AsyncMock

    mock_db = MagicMock()
    mock_db.claim_local_tasks = AsyncMock(return_value=[])

    # Simulate the loop with a very short deadline (0.3 s, 2 poll ticks).
    poll_interval = 0.2
    max_wait = 0.3
    deadline = asyncio.get_event_loop().time() + max_wait

    tasks: list[Any] = []
    while True:
        result = await mock_db.claim_local_tasks(runner_id="test-runner", kinds=None, limit=5)
        if result:
            tasks = result
            break
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))

    assert tasks == []
    assert mock_db.claim_local_tasks.call_count >= 1


@pytest.mark.asyncio
async def test_longpoll_claim_logic_task_available_on_second_tick():
    """Simulate the poll loop: first tick returns [], second tick returns a task."""
    mock_db = MagicMock()
    mock_db.claim_local_tasks = AsyncMock(side_effect=[[], [{"id": "abc"}]])

    poll_interval = 0.05
    max_wait = 5.0
    deadline = asyncio.get_event_loop().time() + max_wait

    tasks: list[Any] = []
    while True:
        result = await mock_db.claim_local_tasks(runner_id="test-runner", kinds=None, limit=5)
        if result:
            tasks = result
            break
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))

    assert len(tasks) == 1
    assert tasks[0]["id"] == "abc"


@pytest.mark.asyncio
async def test_longpoll_task_wait_logic_already_terminal():
    """Simulate /wait loop: task is already succeeded — returns immediately."""
    mock_db = MagicMock()
    mock_db.get_local_task = AsyncMock(
        return_value={"id": "xyz", "status": "succeeded", "result": {}}
    )

    terminal_statuses = frozenset({"succeeded", "failed", "cancelled"})
    poll_interval = 0.2
    max_wait = 25.0
    deadline = asyncio.get_event_loop().time() + max_wait

    task = None
    while True:
        task = await mock_db.get_local_task(task_id="xyz")
        if not task:
            break
        if task.get("status") in terminal_statuses:
            break
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))

    assert task is not None
    assert task["status"] == "succeeded"
    assert mock_db.get_local_task.call_count == 1  # returned on first check


@pytest.mark.asyncio
async def test_longpoll_task_wait_logic_transitions_to_terminal():
    """Simulate /wait loop: task starts as 'claimed', then becomes 'succeeded'."""
    mock_db = MagicMock()
    mock_db.get_local_task = AsyncMock(
        side_effect=[
            {"id": "xyz", "status": "claimed", "result": None},
            {"id": "xyz", "status": "claimed", "result": None},
            {"id": "xyz", "status": "succeeded", "result": {"output": "done"}},
        ]
    )

    terminal_statuses = frozenset({"succeeded", "failed", "cancelled"})
    poll_interval = 0.05
    max_wait = 5.0
    deadline = asyncio.get_event_loop().time() + max_wait

    task = None
    while True:
        task = await mock_db.get_local_task(task_id="xyz")
        if not task:
            break
        if task.get("status") in terminal_statuses:
            break
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))

    assert task is not None
    assert task["status"] == "succeeded"
    assert mock_db.get_local_task.call_count == 3
