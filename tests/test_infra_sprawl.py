"""Unit tests for the infra-sprawl tools (Worker C, Day-0 bootstrap).

External HTTP (Northflank, Cloudflare, DeepSeek) and fleet.db are stubbed
so these run with no creds and no Neon dev branch.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mcp_server.tools import infra_sprawl

# ── stubs ────────────────────────────────────────────────────────────────


class _StubDB:
    """Minimal in-memory fleet DB for tool tests."""

    def __init__(self, agents: list[dict[str, Any]] | None = None) -> None:
        self.agents = agents or []
        self.events: list[dict[str, Any]] = []
        self.artifacts: list[dict[str, Any]] = []
        self.pool = self  # for archive_service fleet_agent path

    async def fleet_status(self) -> list[dict[str, Any]]:
        return self.agents

    async def record_event(self, *, agent_name, kind, payload, run_id=None, level="info"):
        self.events.append({
            "agent_name": agent_name, "kind": kind, "payload": payload,
            "run_id": run_id, "level": level,
        })
        return len(self.events)

    async def record_artifact_content(
        self, *, agent_name, kind, name, body, run_id=None, intent=None,
        repo=None, meta=None, send_blocking=False,
    ):
        self.artifacts.append({
            "agent_name": agent_name, "kind": kind, "name": name, "body": body,
            "run_id": run_id, "intent": intent, "repo": repo,
        })
        return len(self.artifacts)

    # pool.acquire() context manager shim
    def acquire(self):
        outer = self

        class _Ctx:
            async def __aenter__(self):
                return outer

            async def __aexit__(self, *a):
                return False

        return _Ctx()

    async def execute(self, *a, **kw):
        return None


class _FakeResp:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeClient:
    """httpx.AsyncClient stand-in routed by path → payload mapping."""

    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    async def get(self, path: str, *, params=None) -> _FakeResp:
        self.calls.append(("GET", path))
        if ("GET", path) not in self.routes:
            return _FakeResp({"data": []})
        return _FakeResp(self.routes[("GET", path)])

    async def post(self, path: str, *, json=None) -> _FakeResp:
        self.calls.append(("POST", path))
        payload = self.routes.get(("POST", path), {"ok": True})
        return _FakeResp(payload)

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def stub_db(monkeypatch):
    db = _StubDB(agents=[
        {
            "name": "motto-director",
            "kind": "variable",
            "deploy_target": "northflank",
            "version": "0.4.0",
            "last_seen_at": datetime.now(UTC).isoformat(),
            "last_run": {"id": "r1", "kind": "tick", "status": "success",
                         "started_at": datetime.now(UTC).isoformat(),
                         "intent": "perceive"},
            "open_intents": 0,
        },
        {
            "name": "motto-old-agent",
            "kind": "variable",
            "deploy_target": "northflank",
            "version": "0.1.0",
            "last_seen_at": (datetime.now(UTC) - timedelta(days=40)).isoformat(),
            "last_run": None,
            "open_intents": 0,
        },
    ])

    class _ServerStub:
        pass

    server_stub = _ServerStub()
    server_stub.db = db
    import sys
    monkeypatch.setitem(sys.modules, "mcp_server.server", server_stub)
    return db


@pytest.fixture
def no_external_apis(monkeypatch):
    monkeypatch.delenv("NORTHFLANK_API_TOKEN", raising=False)
    monkeypatch.delenv("NORTHFLANK_API_KEY", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)


# ── list_all_services ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_all_services_returns_only_fleet_when_no_creds(stub_db, no_external_apis):
    out = await infra_sprawl.list_all_services()
    kinds = {s["kind"] for s in out}
    assert kinds == {"fleet_agent"}
    assert len(out) == 2
    assert any(s["name"] == "motto-director" for s in out)
    assert any(e["kind"] == "infra_sprawl.list_all_services" for e in stub_db.events)
    assert any(a["kind"] == "infra_sprawl_inventory" for a in stub_db.artifacts)


@pytest.mark.asyncio
async def test_list_all_services_unifies_northflank_and_cloudflare(
    stub_db, no_external_apis, monkeypatch
):
    monkeypatch.setenv("NORTHFLANK_API_TOKEN", "t")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "t")

    nf_routes = {
        ("GET", "/projects"): {"data": {"projects": [{"id": "motto-agents"}]}},
        ("GET", "/projects/motto-agents/services"): {"data": {"services": [
            {"id": "sdr", "name": "sdr", "updatedAt": "2026-05-01T00:00:00Z",
             "vcsData": {"vcsAccountName": "lkmotto", "projectName": "motto-sdr"}},
        ]}},
        ("GET", "/projects/motto-agents/jobs"): {"data": {"jobs": [
            {"id": "nudge", "name": "auto-nudge", "createdAt": "2026-04-01T00:00:00Z"},
        ]}},
    }
    cf_routes = {
        ("GET", "/accounts"): {
            "success": True,
            "result": [{"id": "acc1"}],
        },
        ("GET", "/accounts/acc1/workers/scripts"): {
            "success": True,
            "result": [{"id": "edge-router", "modified_on": "2026-05-10T00:00:00Z"}],
        },
    }

    nf_client = _FakeClient(nf_routes)
    cf_client = _FakeClient(cf_routes)
    monkeypatch.setattr(infra_sprawl, "_nf_client", lambda: nf_client)
    monkeypatch.setattr(infra_sprawl, "_cf_client", lambda: cf_client)

    out = await infra_sprawl.list_all_services()
    kinds = {s["kind"] for s in out}
    assert "northflank_service" in kinds
    assert "northflank_job" in kinds
    assert "cloudflare_worker" in kinds
    assert "fleet_agent" in kinds
    svc = next(s for s in out if s["kind"] == "northflank_service")
    assert svc["repo_link"] == "https://github.com/lkmotto/motto-sdr"
    assert nf_client.closed and cf_client.closed


# ── find_orphans ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_orphans_flags_stale_only(stub_db, no_external_apis):
    out = await infra_sprawl.find_orphans(days_since_run=14)
    names = {c["name"] for c in out}
    assert "motto-old-agent" in names
    assert "motto-director" not in names
    stale = next(c for c in out if c["name"] == "motto-old-agent")
    assert stale["days_idle"] is not None and stale["days_idle"] > 14
    assert any(e["kind"] == "infra_sprawl.find_orphans" for e in stub_db.events)


@pytest.mark.asyncio
async def test_find_orphans_respects_threshold(stub_db, no_external_apis):
    # 100-day threshold should hide the 40-day-old agent.
    out = await infra_sprawl.find_orphans(days_since_run=100)
    assert all(c["name"] != "motto-old-agent" for c in out)


# ── archive_service ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_service_dry_run_records_confirmation_required(stub_db, no_external_apis):
    out = await infra_sprawl.archive_service(name="motto-old-agent", reason="idle")
    assert out["ok"] is True
    assert out["dry_run"] is True
    assert any(
        e["kind"] == "infra_sprawl.archive_service.confirmation_required"
        for e in stub_db.events
    )


@pytest.mark.asyncio
async def test_archive_service_unknown_name_returns_not_found(stub_db, no_external_apis):
    out = await infra_sprawl.archive_service(name="does-not-exist", confirmed=True)
    assert out == {"ok": False, "name": "does-not-exist", "error": "not_found"}


@pytest.mark.asyncio
async def test_archive_service_confirmed_fleet_agent_writes_manifest(
    stub_db, no_external_apis, tmp_path, monkeypatch
):
    monkeypatch.setattr(infra_sprawl, "_repo_root", lambda: tmp_path)
    out = await infra_sprawl.archive_service(
        name="motto-old-agent", reason="sunset", confirmed=True,
    )
    assert out["ok"] is True
    assert out["dry_run"] is False
    manifest_path = tmp_path / "archived" / "motto-old-agent" / "manifest.json"
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["name"] == "motto-old-agent"
    assert manifest["archived_reason"] == "sunset"
    assert any(
        e["kind"] == "infra_sprawl.archive_service.applied" for e in stub_db.events
    )


# ── consolidation_audit ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consolidation_audit_heuristic_only_without_deepseek_key(
    stub_db, no_external_apis, monkeypatch
):
    # Two services that share the "motto-" prefix → one candidate pair.
    stub_db.agents.append({
        "name": "motto-old-twin",
        "kind": "variable",
        "deploy_target": "northflank",
        "version": "0.1.0",
        "last_seen_at": datetime.now(UTC).isoformat(),
        "last_run": None,
        "open_intents": 0,
    })
    clusters = await infra_sprawl.consolidation_audit()
    # At least one heuristic cluster with merge_score=None
    assert any(c["merge_score"] is None for c in clusters)
    assert any(
        e["kind"] == "infra_sprawl.consolidation_audit" for e in stub_db.events
    )
