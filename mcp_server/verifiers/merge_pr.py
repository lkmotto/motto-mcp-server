"""merge_pr verifier: check the merged PR's commit went green on CI.

Strategy:
    1. From the move payload pull repo + pr_number (or merge SHA).
    2. Hit GitHub's combined status / check-runs API for the merge commit.
    3. passed if all required checks succeeded; failed if any failed;
       inconclusive while pending.

Capability requirements:
    - GITHUB_TOKEN env var (already present in the deploy — same one cockpit
      uses to read PRs). If missing, requests it as a capability.
"""

from __future__ import annotations

import os
from typing import Any

from .types import VerifyContext, VerifyResult

_GITHUB_API = "https://api.github.com"


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")


def _headers(tok: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _extract_repo_pr(move: dict[str, Any]) -> tuple[str | None, int | None, str | None]:
    repo = move.get("repo") or ""
    payload = move.get("move_payload") or {}
    if isinstance(payload, str):
        try:
            import json

            payload = json.loads(payload)
        except (ValueError, TypeError):
            payload = {}
    pr_number = payload.get("pr_number") or payload.get("number")
    sha = payload.get("merge_sha") or payload.get("sha")
    if pr_number is not None:
        try:
            pr_number = int(pr_number)
        except (ValueError, TypeError):
            pr_number = None
    return (repo or None, pr_number, sha)


async def verify(move: dict[str, Any], ctx: VerifyContext) -> VerifyResult:
    repo, pr_number, sha = _extract_repo_pr(move)
    if not repo:
        return VerifyResult(
            status="inconclusive",
            verifier="merge_pr.ci_green",
            error="move missing repo",
            evidence={"move_payload_keys": list((move.get("move_payload") or {}).keys())},
        )

    tok = _token()
    if not tok:
        req_id = await ctx.request_capability(
            "github_token",
            "merge_pr verifier needs GITHUB_TOKEN to query PR check-runs",
            repo,
        )
        return VerifyResult(
            status="inconclusive",
            verifier="merge_pr.ci_green",
            error="GITHUB_TOKEN not configured; capability request filed",
            evidence={"capability_request_id": req_id},
        )

    # Resolve commit SHA: prefer payload sha, else fetch from PR.
    if not sha and pr_number is not None:
        pr_url = f"{_GITHUB_API}/repos/{repo}/pulls/{pr_number}"
        pr = await ctx.http_get(pr_url, headers=_headers(tok))
        if pr.status_code == 200:
            data = pr.json()
            # Prefer merge_commit_sha if merged, else head.sha
            sha = data.get("merge_commit_sha") or (data.get("head") or {}).get("sha")
        else:
            return VerifyResult(
                status="inconclusive",
                verifier="merge_pr.ci_green",
                error=f"github pr fetch returned {pr.status_code}",
                evidence={"repo": repo, "pr": pr_number},
            )

    if not sha:
        return VerifyResult(
            status="inconclusive",
            verifier="merge_pr.ci_green",
            error="no commit SHA resolvable from move",
            evidence={"repo": repo, "pr": pr_number},
        )

    # Combined status (legacy) + check-runs (modern). Most motto-* repos
    # use GitHub Actions which surface as check-runs.
    runs_url = f"{_GITHUB_API}/repos/{repo}/commits/{sha}/check-runs"
    cr = await ctx.http_get(runs_url, headers=_headers(tok))
    if cr.status_code != 200:
        return VerifyResult(
            status="inconclusive",
            verifier="merge_pr.ci_green",
            error=f"check-runs api returned {cr.status_code}",
            evidence={"repo": repo, "sha": sha},
        )

    body = cr.json()
    runs = body.get("check_runs") or []
    if not runs:
        return VerifyResult(
            status="inconclusive",
            verifier="merge_pr.ci_green",
            evidence={"repo": repo, "sha": sha, "note": "no check-runs registered"},
        )

    statuses = [(r.get("name"), r.get("status"), r.get("conclusion")) for r in runs]
    pending = [s for s in statuses if s[1] != "completed"]
    failed = [
        s for s in statuses if s[1] == "completed" and s[2] not in ("success", "skipped", "neutral")
    ]

    if pending:
        return VerifyResult(
            status="inconclusive",
            verifier="merge_pr.ci_green",
            evidence={"repo": repo, "sha": sha, "pending": pending, "all": statuses},
        )

    if failed:
        return VerifyResult(
            status="failed",
            verifier="merge_pr.ci_green",
            evidence={"repo": repo, "sha": sha, "failed": failed, "all": statuses},
            kpi_delta={"ci_green_rate": -1},
        )

    return VerifyResult(
        status="passed",
        verifier="merge_pr.ci_green",
        evidence={"repo": repo, "sha": sha, "checks": statuses},
        kpi_delta={"ci_green_rate": +1},
    )
