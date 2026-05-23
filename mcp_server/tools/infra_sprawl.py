"""Infra-sprawl control MCP tools (Day-0 bootstrap, Worker C).

Tools:
  list_all_services     - unified NF + CF service listing
  find_orphans          - stale/unused service detection
  archive_service       - dry-run+safe service archival
  consolidation_audit   - LLM-driven duplication analysis

Every state-changing tool calls db.record_event and db.record_artifact_content
so the cockpit can replay decisions. Read-only audits (list_all_services,
find_orphans, consolidation_audit) still record an event + artifact so we
have a paper trail of inventory snapshots.

External APIs:
  - Northflank REST   NORTHFLANK_API_TOKEN (services + cron jobs)
  - Cloudflare REST   CLOUDFLARE_API_TOKEN (Workers)
  - DeepSeek chat     DEEPSEEK_API_KEY     (consolidation grading)

Maritime.sh is dead (see AGENTS.md). We surface variable fleet agents from
fleet.agents instead, which is what survived the Maritime sunset.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from datetime import UTC, datetime
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ── Internal helpers ──────────────────────────────────────────────────────


def _get_db():
    """Lazy import to avoid circular module load with mcp_server.server."""
    from mcp_server import server as _server_module
    return _server_module.db


_NORTHFLANK_API = "https://api.northflank.com/v1"
_CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
_DEEPSEEK_API = "https://api.deepseek.com/v1/chat/completions"
_HTTP_TIMEOUT = 30.0
_AGENT_NAME = "infra-sprawl"


def _repo_root() -> pathlib.Path:
    """motto-mcp-server repo root (parent of mcp_server/)."""
    return pathlib.Path(__file__).resolve().parents[2]


async def _record_event(
    kind: str,
    payload: dict[str, Any],
    run_id: str | None = None,
    level: str = "info",
) -> int | None:
    """Best-effort event recording. Never raises — sprawl tools must keep
    working even when fleet DB is briefly unreachable."""
    try:
        db = _get_db()
        return await db.record_event(
            agent_name=_AGENT_NAME,
            kind=kind,
            payload=payload,
            run_id=run_id,
            level=level,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("infra_sprawl: record_event(%s) failed: %s", kind, exc)
        return None


async def _record_artifact(
    kind: str,
    body: dict[str, Any] | list[Any] | str,
    name: str | None = None,
    run_id: str | None = None,
    intent: str | None = None,
) -> int | None:
    try:
        text = body if isinstance(body, str) else json.dumps(body, default=str, indent=2)
        db = _get_db()
        return await db.record_artifact_content(
            agent_name=_AGENT_NAME,
            kind=kind,
            name=name,
            body=text,
            run_id=run_id,
            intent=intent,
            repo="lkmotto/motto-mcp-server",
            meta=None,
            send_blocking=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("infra_sprawl: record_artifact_content(%s) failed: %s", kind, exc)
        return None


def _iso(ts: Any) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.astimezone(UTC).isoformat()
    return str(ts)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except Exception:
        return None


async def _nf_get(client: httpx.AsyncClient, path: str) -> Any:
    resp = await client.get(path)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and set(data.keys()) == {"data"}:
        return data["data"]
    return data


async def _cf_get(client: httpx.AsyncClient, path: str) -> Any:
    resp = await client.get(path)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and "success" in data:
        if not data.get("success", False):
            errs = "; ".join(str(e) for e in (data.get("errors") or []))
            raise RuntimeError(f"Cloudflare {path}: {errs}")
        return data.get("result")
    return data


def _service_repo_link(svc: dict[str, Any]) -> str | None:
    """Best-effort extraction of a git remote from a Northflank service spec."""
    vcs = (svc.get("vcsData") or svc.get("vcs") or {})
    if not isinstance(vcs, dict):
        return None
    url = vcs.get("projectUrl") or vcs.get("vcsLink") or vcs.get("repoUrl")
    if isinstance(url, str) and url.startswith("http"):
        return url
    account = vcs.get("vcsAccountName") or vcs.get("accountName")
    project = vcs.get("projectName") or vcs.get("repoName")
    if account and project:
        return f"https://github.com/{account}/{project}"
    return None


def _service_last_deployed(svc: dict[str, Any]) -> str | None:
    deploy = svc.get("deployment") or {}
    if isinstance(deploy, dict):
        for key in ("deployedAt", "lastDeployment", "createdAt"):
            val = deploy.get(key)
            if val:
                return _iso(val)
    for key in ("updatedAt", "lastDeployedAt", "createdAt"):
        val = svc.get(key)
        if val:
            return _iso(val)
    return None


async def _collect_northflank(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Enumerate every service + cron job across every Northflank project."""
    out: list[dict[str, Any]] = []
    try:
        projects_data = await _nf_get(client, "/projects")
    except Exception as exc:  # noqa: BLE001
        logger.warning("infra_sprawl: list_projects failed: %s", exc)
        return out
    projects = (
        projects_data if isinstance(projects_data, list)
        else projects_data.get("projects", [])
    )
    for proj in projects or []:
        pid = proj.get("id") or proj.get("name")
        if not pid:
            continue
        try:
            svcs_data = await _nf_get(client, f"/projects/{pid}/services")
            svcs = svcs_data if isinstance(svcs_data, list) else svcs_data.get("services", [])
            for svc in svcs or []:
                out.append({
                    "name": svc.get("name") or svc.get("id"),
                    "kind": "northflank_service",
                    "project_id": pid,
                    "service_id": svc.get("id"),
                    "last_deployed_at": _service_last_deployed(svc),
                    "has_recent_runs": None,
                    "repo_link": _service_repo_link(svc),
                    "status": (svc.get("status") or {}).get("deployment")
                              if isinstance(svc.get("status"), dict) else svc.get("status"),
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("infra_sprawl: list_services(%s) failed: %s", pid, exc)
        try:
            jobs_data = await _nf_get(client, f"/projects/{pid}/jobs")
            jobs = jobs_data if isinstance(jobs_data, list) else jobs_data.get("jobs", [])
            for job in jobs or []:
                out.append({
                    "name": job.get("name") or job.get("id"),
                    "kind": "northflank_job",
                    "project_id": pid,
                    "service_id": job.get("id"),
                    "last_deployed_at": _service_last_deployed(job),
                    "has_recent_runs": None,
                    "repo_link": _service_repo_link(job),
                    "status": (job.get("status") or {}).get("deployment")
                              if isinstance(job.get("status"), dict) else job.get("status"),
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("infra_sprawl: list_jobs(%s) failed: %s", pid, exc)
    return out


async def _collect_cloudflare(client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """Enumerate Workers across every Cloudflare account."""
    out: list[dict[str, Any]] = []
    try:
        accounts = await _cf_get(client, "/accounts")
    except Exception as exc:  # noqa: BLE001
        logger.warning("infra_sprawl: list_accounts failed: %s", exc)
        return out
    for acc in accounts or []:
        aid = acc.get("id")
        if not aid:
            continue
        try:
            scripts = await _cf_get(client, f"/accounts/{aid}/workers/scripts") or []
            for w in scripts:
                out.append({
                    "name": w.get("id") or w.get("name"),
                    "kind": "cloudflare_worker",
                    "project_id": aid,
                    "service_id": w.get("id"),
                    "last_deployed_at": _iso(w.get("modified_on") or w.get("created_on")),
                    "has_recent_runs": None,
                    "repo_link": None,
                    "status": "deployed",
                })
        except Exception as exc:  # noqa: BLE001
            logger.warning("infra_sprawl: list_workers(%s) failed: %s", aid, exc)
    return out


async def _collect_fleet_agents() -> list[dict[str, Any]]:
    """Variable fleet agents from fleet.agents (Maritime's successor)."""
    try:
        db = _get_db()
        agents = await db.fleet_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("infra_sprawl: fleet_status failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for a in agents or []:
        last_run = a.get("last_run") or {}
        last_started = last_run.get("started_at") if isinstance(last_run, dict) else None
        out.append({
            "name": a.get("name"),
            "kind": "fleet_agent",
            "project_id": a.get("deploy_target"),
            "service_id": a.get("name"),
            "last_deployed_at": a.get("last_seen_at"),
            "has_recent_runs": bool(last_started),
            "repo_link": None,
            "status": (last_run.get("status") if isinstance(last_run, dict) else None),
            "last_seen_at": a.get("last_seen_at"),
        })
    return out


def _nf_client() -> httpx.AsyncClient | None:
    token = os.environ.get("NORTHFLANK_API_TOKEN") or os.environ.get("NORTHFLANK_API_KEY")
    if not token:
        return None
    return httpx.AsyncClient(
        base_url=_NORTHFLANK_API,
        timeout=_HTTP_TIMEOUT,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )


def _cf_client() -> httpx.AsyncClient | None:
    token = os.environ.get("CLOUDFLARE_API_TOKEN")
    if not token:
        return None
    return httpx.AsyncClient(
        base_url=_CLOUDFLARE_API,
        timeout=_HTTP_TIMEOUT,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        },
    )


# ── MCP tools ─────────────────────────────────────────────────────────────


async def list_all_services(run_id: str | None = None) -> list[dict[str, Any]]:
    """List all services across Northflank projects, jobs, and Cloudflare
    Workers. Returns unified list with: name, kind, last_deployed_at,
    has_recent_runs, repo_link if discoverable.
    """
    services: list[dict[str, Any]] = []

    nf = _nf_client()
    if nf is not None:
        try:
            services.extend(await _collect_northflank(nf))
        finally:
            await nf.aclose()
    else:
        logger.info("infra_sprawl: NORTHFLANK_API_TOKEN not set; skipping Northflank")

    cf = _cf_client()
    if cf is not None:
        try:
            services.extend(await _collect_cloudflare(cf))
        finally:
            await cf.aclose()
    else:
        logger.info("infra_sprawl: CLOUDFLARE_API_TOKEN not set; skipping Cloudflare")

    services.extend(await _collect_fleet_agents())

    summary = {
        "total": len(services),
        "by_kind": {
            k: sum(1 for s in services if s.get("kind") == k)
            for k in {s.get("kind") for s in services if s.get("kind")}
        },
    }
    await _record_event("infra_sprawl.list_all_services", summary, run_id=run_id)
    await _record_artifact(
        "infra_sprawl_inventory",
        services,
        name="all_services.json",
        run_id=run_id,
        intent="enumerate fleet inventory",
    )
    return services


async def find_orphans(
    days_since_run: int = 14,
    days_since_commit: int = 60,
    run_id: str | None = None,
) -> list[dict[str, Any]]:
    """Flag services with no fleet runs / heartbeats inside `days_since_run`
    days. `days_since_commit` is reserved for a future GH-blame pass; today we
    only return runtime-staleness candidates.
    """
    services = await list_all_services(run_id=run_id)
    now = datetime.now(UTC)
    candidates: list[dict[str, Any]] = []
    for svc in services:
        last_iso = svc.get("last_seen_at") or svc.get("last_deployed_at")
        last = _parse_iso(last_iso) if isinstance(last_iso, str) else None
        days_idle: float | None = None
        if last is not None:
            days_idle = round((now - last).total_seconds() / 86400.0, 2)
        is_orphan = (last is None) or (days_idle is not None and days_idle > days_since_run)
        if not is_orphan:
            continue
        candidates.append({
            "name": svc.get("name"),
            "kind": svc.get("kind"),
            "project_id": svc.get("project_id"),
            "service_id": svc.get("service_id"),
            "last_activity_at": last_iso,
            "days_idle": days_idle,
            "repo_link": svc.get("repo_link"),
            "reason": "never_active" if last is None else f"idle_{days_idle}d",
        })

    summary = {
        "candidates": len(candidates),
        "threshold_days_since_run": days_since_run,
        "threshold_days_since_commit": days_since_commit,
        "scanned": len(services),
    }
    await _record_event("infra_sprawl.find_orphans", summary, run_id=run_id)
    await _record_artifact(
        "infra_sprawl_orphans",
        {"summary": summary, "candidates": candidates},
        name="orphans.json",
        run_id=run_id,
        intent="flag idle services for archive review",
    )
    return candidates


async def archive_service(
    name: str,
    reason: str = "",
    confirmed: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Archive a service. Default is dry-run (confirmation_required event).
    Set confirmed=true to actually stop the service via Northflank API and
    write a manifest to `archived/<name>/manifest.json` in this repo.
    """
    if not name:
        raise ValueError("archive_service: name is required")

    services = await list_all_services(run_id=run_id)
    match = next((s for s in services if s.get("name") == name), None)
    if match is None:
        payload = {"name": name, "error": "not_found", "reason": reason}
        await _record_event("infra_sprawl.archive_service.not_found", payload,
                            run_id=run_id, level="warn")
        return {"ok": False, "name": name, "error": "not_found"}

    target_platform = match.get("kind") or "unknown"

    if not confirmed:
        payload = {
            "name": name,
            "kind": target_platform,
            "project_id": match.get("project_id"),
            "service_id": match.get("service_id"),
            "reason": reason,
            "dry_run": True,
        }
        await _record_event(
            "infra_sprawl.archive_service.confirmation_required",
            payload,
            run_id=run_id,
            level="warn",
        )
        return {
            "ok": True,
            "dry_run": True,
            "name": name,
            "kind": target_platform,
            "requires": "confirmed=True",
            "service": match,
        }

    archive_dir = _repo_root() / "archived" / name
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("infra_sprawl: mkdir(archived/%s) failed: %s", name, exc)

    manifest = {
        "name": name,
        "kind": target_platform,
        "project_id": match.get("project_id"),
        "service_id": match.get("service_id"),
        "archived_at": datetime.now(UTC).isoformat(),
        "archived_reason": reason,
        "last_known_state": match,
    }

    stopped = False
    stop_error: str | None = None
    if target_platform == "northflank_service":
        nf = _nf_client()
        if nf is None:
            stop_error = "NORTHFLANK_API_TOKEN not set"
        else:
            try:
                pid = match.get("project_id")
                sid = match.get("service_id")
                resp = await nf.post(
                    f"/projects/{pid}/services/{sid}/actions/stop",
                )
                stopped = resp.status_code < 400
                if not stopped:
                    stop_error = f"northflank stop {resp.status_code}: {resp.text[:200]}"
            except Exception as exc:  # noqa: BLE001
                stop_error = f"northflank stop exception: {exc}"
            finally:
                await nf.aclose()
    elif target_platform == "northflank_job":
        stop_error = "northflank_job: no stop action; archived manifest only"
    elif target_platform == "cloudflare_worker":
        stop_error = "cloudflare_worker: requires explicit delete; archived manifest only"
    elif target_platform == "fleet_agent":
        try:
            db = _get_db()
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE fleet.agents SET version = COALESCE(version,'') || ' (archived)' "
                    "WHERE name = $1",
                    name,
                )
            stopped = True
        except Exception as exc:  # noqa: BLE001
            stop_error = f"fleet_agent archive exception: {exc}"

    manifest["stopped"] = stopped
    manifest["stop_error"] = stop_error
    try:
        manifest_path = archive_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
        manifest["manifest_path"] = str(manifest_path.relative_to(_repo_root()))
    except Exception as exc:  # noqa: BLE001
        manifest["manifest_path"] = None
        manifest["manifest_error"] = str(exc)

    await _record_event(
        "infra_sprawl.archive_service.applied",
        {
            "name": name,
            "kind": target_platform,
            "stopped": stopped,
            "stop_error": stop_error,
            "reason": reason,
        },
        run_id=run_id,
        level="info" if stopped else "warn",
    )
    await _record_artifact(
        "infra_sprawl_archive_manifest",
        manifest,
        name=f"{name}/manifest.json",
        run_id=run_id,
        intent=f"archive {name}: {reason}",
    )
    return {"ok": True, "dry_run": False, **manifest}


async def _deepseek_grade(client: httpx.AsyncClient, prompt: str) -> dict[str, Any]:
    """Call DeepSeek V4-Flash chat for consolidation grading. Returns parsed
    JSON when the model emits a JSON object; otherwise returns raw text under
    the `raw` key."""
    payload = {
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an infrastructure consolidation auditor. For each "
                    "pair of services you receive, respond with strict JSON: "
                    '{"merge_score":0..1,"merged_name":"...","rationale":"..."}.'
                    " Higher score means stronger consolidation case."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }
    resp = await client.post("", json=payload)
    resp.raise_for_status()
    data = resp.json()
    text = ""
    try:
        text = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        text = ""
    try:
        return json.loads(text) if text else {"raw": data}
    except json.JSONDecodeError:
        return {"raw": text}


def _summarize_for_audit(s: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": s.get("name"),
        "kind": s.get("kind"),
        "project_id": s.get("project_id"),
        "repo_link": s.get("repo_link"),
        "last_activity_at": s.get("last_deployed_at") or s.get("last_seen_at"),
    }


def _candidate_pairs(services: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Pair services whose names share a normalized prefix (cheap shortlist)."""
    by_prefix: dict[str, list[dict[str, Any]]] = {}
    for s in services:
        n = (s.get("name") or "").lower()
        if not n:
            continue
        parts = n.replace("_", "-").split("-")
        prefix = "-".join(parts[:2]) if len(parts) >= 2 else parts[0]
        by_prefix.setdefault(prefix, []).append(s)
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for group in by_prefix.values():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                pairs.append((group[i], group[j]))
                if len(pairs) >= 25:
                    return pairs
    return pairs


async def consolidation_audit(run_id: str | None = None) -> list[dict[str, Any]]:
    """LLM-driven audit (DeepSeek). Groups similar service-name prefixes,
    grades each pair, and returns clusters with merge_score >= 0.5."""
    services = await list_all_services(run_id=run_id)
    pairs = _candidate_pairs(services)
    api_key = os.environ.get("DEEPSEEK_API_KEY")

    clusters: list[dict[str, Any]] = []
    if not pairs:
        await _record_event(
            "infra_sprawl.consolidation_audit",
            {"clusters": 0, "pairs_scanned": 0, "reason": "no_candidate_pairs"},
            run_id=run_id,
        )
        return clusters

    if not api_key:
        for a, b in pairs:
            clusters.append({
                "members": [_summarize_for_audit(a), _summarize_for_audit(b)],
                "merge_score": None,
                "merged_name": None,
                "rationale": "heuristic shortlist (DEEPSEEK_API_KEY not set)",
            })
        await _record_event(
            "infra_sprawl.consolidation_audit",
            {"clusters": len(clusters), "pairs_scanned": len(pairs), "graded": False},
            run_id=run_id,
            level="warn",
        )
        await _record_artifact(
            "infra_sprawl_consolidation",
            {"clusters": clusters, "graded": False},
            name="consolidation_audit.json",
            run_id=run_id,
            intent="heuristic-only consolidation audit",
        )
        return clusters

    async with httpx.AsyncClient(
        base_url=_DEEPSEEK_API,
        timeout=_HTTP_TIMEOUT,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
        },
    ) as client:
        for a, b in pairs:
            prompt = json.dumps({
                "service_a": _summarize_for_audit(a),
                "service_b": _summarize_for_audit(b),
            }, default=str)
            try:
                graded = await _deepseek_grade(client, prompt)
            except Exception as exc:  # noqa: BLE001
                graded = {"error": str(exc)}
            score = graded.get("merge_score")
            if isinstance(score, (int, float)) and score >= 0.5:
                clusters.append({
                    "members": [_summarize_for_audit(a), _summarize_for_audit(b)],
                    "merge_score": float(score),
                    "merged_name": graded.get("merged_name"),
                    "rationale": graded.get("rationale"),
                })

    await _record_event(
        "infra_sprawl.consolidation_audit",
        {"clusters": len(clusters), "pairs_scanned": len(pairs), "graded": True},
        run_id=run_id,
    )
    await _record_artifact(
        "infra_sprawl_consolidation",
        {"clusters": clusters, "pairs_scanned": len(pairs), "graded": True},
        name="consolidation_audit.json",
        run_id=run_id,
        intent="LLM-graded consolidation audit",
    )
    return clusters
