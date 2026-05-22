"""Tests for claim_next_step / release_claimed_step MCP tools.

These tests target the droid-side queue handle into public.pending_moves.
The table is owned by motto-director's migrations; this suite assumes the
table exists in the target Neon DB (motto-director's 0005_pending_moves
migration has been applied). When NEON_TEST_DATABASE_URL is unset or the
table is missing, every test in the file is skipped.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import _call, requires_db

pytestmark = pytest.mark.asyncio


async def _pending_moves_exists(db) -> bool:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = 'pending_moves'
            )
            """
        )


async def _wipe(db) -> None:
    async with db.pool.acquire() as conn:
        await conn.execute("DELETE FROM public.pending_moves WHERE repo LIKE 'test/%'")


async def _insert_move(
    db,
    *,
    repo: str = "test/repo",
    kind: str = "factory_droid",
    title: str = "t",
    rationale: str = "r",
    intent: str = "i",
    priority: int = 1,
    status: str = "approved",
    move_payload: dict | None = None,
) -> int:
    async with db.pool.acquire() as conn:
        return await conn.fetchval(
            """
            INSERT INTO public.pending_moves
                (repo, kind, title, rationale, intent, priority,
                 move_payload, status)
            VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8)
            RETURNING id
            """,
            repo, kind, title, rationale, intent, priority,
            json.dumps(move_payload or {"task": "x"}),
            status,
        )


@pytest.fixture
async def pending_db(db):
    if not await _pending_moves_exists(db):
        pytest.skip("public.pending_moves not present in test DB")
    await _wipe(db)
    yield db
    await _wipe(db)


# ---------------------------------------------------------------------------
# DB-method coverage
# ---------------------------------------------------------------------------


@requires_db
async def test_claim_next_step_empty_queue_returns_empty_list(pending_db, server):
    result = await _call(server, "claim_next_step", runner_id="droid-1", limit=1)
    assert result == {"ok": True, "claimed": [], "count": 0}


@requires_db
async def test_claim_next_step_claims_highest_priority_first(pending_db, server):
    await _insert_move(pending_db, priority=1, title="low")
    await _insert_move(pending_db, priority=3, title="high")
    await _insert_move(pending_db, priority=2, title="mid")

    result = await _call(server, "claim_next_step", runner_id="droid-1", limit=1)
    assert result["ok"] is True
    assert result["count"] == 1
    assert result["claimed"][0]["title"] == "high"
    assert result["claimed"][0]["status"] == "claimed"

    # Verify DB state
    async with pending_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, claimed_by FROM public.pending_moves WHERE title = 'high'"
        )
        assert row["status"] == "claimed"
        assert row["claimed_by"] == "droid-1"


@requires_db
async def test_claim_next_step_respects_kinds_filter(pending_db, server):
    await _insert_move(pending_db, kind="factory_droid", title="fd")
    await _insert_move(pending_db, kind="file_issue", title="fi")
    await _insert_move(pending_db, kind="merge_pr", title="mp")

    result = await _call(
        server, "claim_next_step",
        runner_id="droid-1", kinds=["factory_droid"], limit=5,
    )
    assert result["count"] == 1
    assert result["claimed"][0]["kind"] == "factory_droid"


@requires_db
async def test_claim_next_step_skips_already_claimed_rows(pending_db, server):
    id_a = await _insert_move(pending_db, priority=2, title="a")
    id_b = await _insert_move(pending_db, priority=1, title="b")

    r1 = await _call(server, "claim_next_step", runner_id="droid-1", limit=1)
    r2 = await _call(server, "claim_next_step", runner_id="droid-2", limit=1)

    assert r1["count"] == 1 and r2["count"] == 1
    assert r1["claimed"][0]["id"] == id_a
    assert r2["claimed"][0]["id"] == id_b


@requires_db
async def test_claim_next_step_rejects_invalid_limit(pending_db, server):
    r0 = await _call(server, "claim_next_step", runner_id="droid-1", limit=0)
    r11 = await _call(server, "claim_next_step", runner_id="droid-1", limit=11)
    assert r0["ok"] is False
    assert r11["ok"] is False


@requires_db
async def test_release_claimed_step_returns_to_approved(pending_db, server):
    move_id = await _insert_move(pending_db, title="x")
    claimed = await _call(server, "claim_next_step", runner_id="droid-1", limit=1)
    assert claimed["count"] == 1
    assert claimed["claimed"][0]["id"] == move_id

    released = await _call(
        server, "release_claimed_step",
        move_id=move_id, runner_id="droid-1", reason="ci-down",
    )
    assert released == {"ok": True, "released": True}

    async with pending_db.pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, claimed_by, claimed_at FROM public.pending_moves WHERE id = $1",
            move_id,
        )
        assert row["status"] == "approved"
        assert row["claimed_by"] is None
        assert row["claimed_at"] is None


@requires_db
async def test_release_claimed_step_idempotent(pending_db, server):
    move_id = await _insert_move(pending_db, title="y")
    await _call(server, "claim_next_step", runner_id="droid-1", limit=1)
    first = await _call(
        server, "release_claimed_step",
        move_id=move_id, runner_id="droid-1", reason="",
    )
    second = await _call(
        server, "release_claimed_step",
        move_id=move_id, runner_id="droid-1", reason="",
    )
    assert first == {"ok": True, "released": True}
    assert second == {"ok": True, "released": False}
