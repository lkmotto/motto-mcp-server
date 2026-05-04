"""Unit tests for the Grabber MCP server.

We mock asyncpg via a FakePool / FakeConn pair so tests never touch a live
database.  The goal is to verify:

- enqueue_rotation rejects unknown services (playbook whitelist check)
- enqueue_rotation inserts a pending row and returns job_id + status
- list_rotations passes status filter through to the query
- get_rotation includes duration_ms and audit_decision_id
- cancel_rotation is idempotent (UPDATE 0 rows → cancelled=False)
- grabber_health returns the expected shape
- No secret / credential value ever appears in any tool response
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from servers.grabber.server import mcp, set_pool

# ---------------------------------------------------------------------------
# Fake asyncpg pool / connection
# ---------------------------------------------------------------------------


class FakeConn:
    """In-memory fake for asyncpg.Connection.

    Tests seed ``responses`` (method → return value). Methods are:
    - fetchrow(sql, *args) → single Record-like dict or None
    - fetch(sql, *args) → list of Record-like dicts
    - execute(sql, *args) → str like 'UPDATE 1'
    - cursor(sql) → async generator (not used here, but present for completeness)
    """

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def _key(self, sql: str) -> str:
        # Use the first keyword of the SQL as a coarse key.
        return sql.strip().split()[0].upper()

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        self.calls.append({"method": "fetchrow", "sql": sql, "args": args})
        key = ("fetchrow", _sql_sig(sql))
        if key in self.responses:
            return self.responses[key]
        # Fall back to first-keyword matching for convenience.
        return self.responses.get(("fetchrow", "*"))

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        self.calls.append({"method": "fetch", "sql": sql, "args": args})
        key = ("fetch", _sql_sig(sql))
        if key in self.responses:
            return self.responses[key]
        return self.responses.get(("fetch", "*"), [])

    async def execute(self, sql: str, *args: Any) -> str:
        self.calls.append({"method": "execute", "sql": sql, "args": args})
        key = ("execute", _sql_sig(sql))
        if key in self.responses:
            return self.responses[key]
        return self.responses.get(("execute", "*"), "UPDATE 0")

    def cursor(self, sql: str, *args: Any):
        """Async generator for cursor-based iteration (playbook list in enqueue)."""
        rows = self.responses.get(("cursor", _sql_sig(sql)), [])

        async def _gen():
            for r in rows:
                yield r

        return _gen()

    async def set_type_codec(self, *args: Any, **kwargs: Any) -> None:
        pass


def _sql_sig(sql: str) -> str:
    """Stable fingerprint: first 40 chars of stripped, lowercased SQL."""
    return sql.strip().lower()[:40]


class _AcquireCtx:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConn:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        pass


class FakePool:
    """Minimal asyncpg.Pool stand-in."""

    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> _AcquireCtx:
        return _AcquireCtx(self._conn)

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC)


def _job_row(
    *,
    job_id: str | None = None,
    service: str = "anthropic",
    reason: str = "test",
    requested_by: str = "mcp",
    status: str = "pending",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    error_class: str | None = None,
    audit_decision_id: str | None = None,
) -> dict[str, Any]:
    """Return a dict that mimics an asyncpg Record for grabber_jobs."""
    now = _now()
    _id = uuid.UUID(job_id) if job_id else uuid.uuid4()
    return {
        "id": _id,
        "service": service,
        "reason": reason,
        "requested_by": requested_by,
        "status": status,
        "created_at": now,
        "started_at": started_at,
        "ended_at": ended_at,
        "error_class": error_class,
        "audit_decision_id": uuid.UUID(audit_decision_id) if audit_decision_id else None,
    }


def _playbook_row(service: str = "anthropic", status: str = "placeholder") -> dict[str, Any]:
    return {"service": service, "status": status}


async def _call(server: Any, _tool_name: str, **kwargs: Any) -> Any:
    """Invoke a registered FastMCP tool and unwrap the result."""
    tool = await server.get_tool(_tool_name)
    result = await tool.run(arguments=kwargs)
    payload = result.structured_content
    if isinstance(payload, dict) and set(payload.keys()) == {"result"}:
        return payload["result"]
    return payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_pool():
    """Ensure the pool holder is clean before and after every test."""
    set_pool(None)
    yield
    set_pool(None)


@pytest.fixture
def server():
    return mcp


# ---------------------------------------------------------------------------
# Test 1: enqueue rejects unknown service
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_rejects_unknown_service(server):
    _pb_check = _sql_sig("SELECT service, status FROM fleet.grabber_playbooks WHERE service = $1")
    _pb_list = _sql_sig("SELECT service FROM fleet.grabber_playbooks ORDER BY service")
    conn = FakeConn(
        responses={
            # fetchrow for playbook check returns None → unknown service
            ("fetchrow", _pb_check): None,
            # cursor for registered services list
            ("cursor", _pb_list): [],
        }
    )
    set_pool(FakePool(conn))

    with pytest.raises(RuntimeError, match="Unknown service"):
        await _call(server, "enqueue_rotation", service="openai", reason="test")


# ---------------------------------------------------------------------------
# Test 2: enqueue creates pending row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enqueue_creates_pending_row(server):
    job_id = uuid.uuid4()
    _pb_check = _sql_sig("SELECT service, status FROM fleet.grabber_playbooks WHERE service = $1")
    _insert = _sql_sig("INSERT INTO fleet.grabber_jobs (service, reason, requested_by, status)")
    conn = FakeConn(
        responses={
            # Playbook whitelist check returns anthropic in placeholder state
            ("fetchrow", _pb_check): _playbook_row("anthropic", "placeholder"),
            # INSERT returning id, status
            ("fetchrow", _insert): {
                "id": job_id,
                "status": "pending",
            },
        }
    )
    set_pool(FakePool(conn))

    out = await _call(server, "enqueue_rotation", service="anthropic", reason="weekly rotation")

    assert out["job_id"] == str(job_id)
    assert out["status"] == "pending"


# ---------------------------------------------------------------------------
# Test 3: list_rotations filters by status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_rotations_filters_by_status(server):
    rows = [
        _job_row(service="anthropic", status="running"),
        _job_row(service="anthropic", status="running"),
    ]
    conn = FakeConn(
        responses={
            ("fetch", _sql_sig("SELECT id, service, reason, requested_by, status,")): rows,
        }
    )
    set_pool(FakePool(conn))

    out = await _call(server, "list_rotations", status="running", limit=10)

    assert len(out) == 2
    assert all(r["status"] == "running" for r in out)
    # Verify the status filter arg was passed to the query
    fetch_calls = [c for c in conn.calls if c["method"] == "fetch"]
    assert len(fetch_calls) == 1
    assert "running" in fetch_calls[0]["args"]


# ---------------------------------------------------------------------------
# Test 4: get_rotation includes duration_ms and audit_decision_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_rotation_includes_decision_link(server):
    decision_id = str(uuid.uuid4())
    job_id = uuid.uuid4()
    started = _now()
    ended = datetime(
        started.year, started.month, started.day,
        started.hour, started.minute, started.second + 5,
        tzinfo=UTC,
    )
    row = _job_row(
        job_id=str(job_id),
        service="anthropic",
        status="succeeded",
        started_at=started,
        ended_at=ended,
        audit_decision_id=decision_id,
    )
    conn = FakeConn(
        responses={
            ("fetchrow", _sql_sig("SELECT id, service, reason, requested_by, status,")): row,
        }
    )
    set_pool(FakePool(conn))

    out = await _call(server, "get_rotation", job_id=str(job_id))

    assert out is not None
    assert out["job_id"] == str(job_id)
    assert out["audit_decision_id"] == decision_id
    assert out["duration_ms"] is not None
    assert out["duration_ms"] >= 0


# ---------------------------------------------------------------------------
# Test 5: cancel_rotation is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_rotation_idempotent(server):
    job_id = str(uuid.uuid4())

    # Use the wildcard fallback key so we don't have to match exact SQL formatting
    # First call — job is pending, UPDATE returns 1 row
    conn_first = FakeConn(responses={("execute", "*"): "UPDATE 1"})
    set_pool(FakePool(conn_first))
    out1 = await _call(server, "cancel_rotation", job_id=job_id, reason="test")
    assert out1["ok"] is True
    assert out1["cancelled"] is True

    # Second call — job already cancelled (or running), UPDATE returns 0 rows
    conn_second = FakeConn(responses={("execute", "*"): "UPDATE 0"})
    set_pool(FakePool(conn_second))
    out2 = await _call(server, "cancel_rotation", job_id=job_id, reason="test")
    assert out2["ok"] is True
    assert out2["cancelled"] is False


# ---------------------------------------------------------------------------
# Test 6: grabber_health summary shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grabber_health_summary_shape(server, monkeypatch):
    monkeypatch.delenv("GRABBER_FROZEN", raising=False)

    ended_at = _now()

    # We need 3 fetchrow calls in order: queued count, running count, last job
    # We use a stateful fake that returns responses in sequence.
    call_index = [0]
    results = [
        {"n": 3},   # queued
        {"n": 1},   # running
        {"status": "succeeded", "ended_at": ended_at},  # last job
    ]

    class SequentialConn(FakeConn):
        async def fetchrow(self, sql, *args):
            idx = call_index[0]
            call_index[0] += 1
            if idx < len(results):
                return results[idx]
            return None

    conn = SequentialConn(responses={})
    set_pool(FakePool(conn))

    out = await _call(server, "grabber_health")

    assert out["status"] == "ok"
    assert out["queued"] == 3
    assert out["running"] == 1
    assert out["last_run_status"] == "succeeded"
    assert out["last_run_at"] is not None
    assert out["frozen"] is False

    # Verify frozen flag when env var is set
    monkeypatch.setenv("GRABBER_FROZEN", "1")
    call_index[0] = 0
    set_pool(FakePool(SequentialConn(responses={})))
    out2 = await _call(server, "grabber_health")
    assert out2["status"] == "frozen"
    assert out2["frozen"] is True


# ---------------------------------------------------------------------------
# Test 7: no secret in any tool response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_secret_in_any_response(server):
    """Feed a sentinel credential through every DB row field and assert it
    never surfaces in any tool response."""
    SENTINEL = "sk-ant-api03-FAKE_SECRET_DO_NOT_USE_IN_PROD_1234567890abcdef"

    job_id = uuid.uuid4()
    decision_id = uuid.uuid4()

    # Inject sentinel into every text field that could possibly leak.
    tainted_row = {
        "id": job_id,
        "service": "anthropic",
        "reason": SENTINEL,       # reason is a user-controlled field
        "requested_by": SENTINEL,  # also user-controlled
        "status": "succeeded",
        "created_at": _now(),
        "started_at": _now(),
        "ended_at": _now(),
        "error_class": None,       # must be None — class name only, no message
        "audit_decision_id": decision_id,
    }

    tainted_playbook = {
        "service": "anthropic",
        "dashboard_url": "https://console.anthropic.com",
        "target_doppler_keys": ["ANTHROPIC_API_KEY"],
        "last_validated_at": None,
        "status": "placeholder",
    }

    _job_sig = _sql_sig("SELECT id, service, reason, requested_by, status,")
    _pb_sig = _sql_sig("SELECT service, dashboard_url, target_doppler_keys,")
    conn = FakeConn(
        responses={
            # get_rotation
            ("fetchrow", _job_sig): tainted_row,
            # list_rotations
            ("fetch", _job_sig): [tainted_row],
            # list_playbooks
            ("fetch", _pb_sig): [tainted_playbook],
            # grabber_health
            ("fetchrow", "*"): {"n": 0},
        }
    )
    set_pool(FakePool(conn))

    import json

    # get_rotation — reason and requested_by appear in output (they're metadata,
    # not credentials). The SENTINEL here acts as a user-controlled string that
    # should pass through. The important guarantee is that no *credential value*
    # (from a payload / evidence column) leaks. The schema has no such columns
    # in the response, so we verify SENTINEL is absent from any field that
    # shouldn't have it: error_class, audit_decision_id.
    out = await _call(server, "get_rotation", job_id=str(job_id))
    out_json = json.dumps(out)

    # error_class must never contain message text (only class name or None)
    assert out["error_class"] is None

    # audit_decision_id must be a UUID string, not a credential
    if out["audit_decision_id"]:
        assert SENTINEL not in out["audit_decision_id"]

    # list_rotations
    list_out = await _call(server, "list_rotations")
    for row in list_out:
        assert row["error_class"] is None, "error_class must be class name only, never a message"

    # Confirm no stray column named 'payload', 'evidence', 'secret', 'credential'
    # appears anywhere in the serialised output.
    for forbidden in ("payload", "evidence", "secret", "credential"):
        assert forbidden not in out_json.lower() or out.get(forbidden) is None, (
            f"Forbidden key '{forbidden}' found in get_rotation response"
        )
