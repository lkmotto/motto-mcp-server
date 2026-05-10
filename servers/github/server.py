"""GitHub MCP server — wraps the GitHub REST API for the Motto fleet.

Exposes repository metadata, issues, pull requests, and Actions secret
provisioning. Read tools are unrestricted; every mutating tool requires
``confirm=True``.

Run with ``python -m servers.github`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.

Auth (lazy, read at first use):
* ``FLEET_PROVISION_PAT`` — preferred PAT for write/secret operations
  (issued by the fleet provisioner workflow with ``repo`` + ``admin:repo_hook``).
* ``GITHUB_PAT`` / ``GITHUB_TOKEN`` — fallback PATs for read tools when
  ``FLEET_PROVISION_PAT`` is not present. Canonical home is Doppler
  ``motto-core/prd``.
"""

from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
DEFAULT_TIMEOUT = 30.0

mcp = FastMCP(
    "motto-github",
    instructions=(
        "GitHub REST API for the Motto fleet — repos, issues, pull requests, "
        "and Actions secret provisioning. Reads are unrestricted; every mutating "
        "tool (create_issue, comment_issue, create_pull, merge_pull, set_secret) "
        "requires confirm=true. PAT read from FLEET_PROVISION_PAT (preferred) or "
        "GITHUB_PAT / GITHUB_TOKEN (fallback) — canonical home: Doppler motto-core/prd."
    ),
)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class GitHubClient:
    """Thin async wrapper around the GitHub REST API.

    Uses a single PAT for both read and write traffic. Callers may pass an
    explicit token; otherwise the env var chain
    ``FLEET_PROVISION_PAT → GITHUB_PAT → GITHUB_TOKEN`` is consulted lazily so
    import-time and pytest collection work without credentials.
    """

    def __init__(self, token: str | None = None) -> None:
        self._token = token or _first_env(
            "FLEET_PROVISION_PAT", "GITHUB_PAT", "GITHUB_TOKEN"
        )
        if not self._token:
            raise RuntimeError(
                "GitHub PAT is required. Set FLEET_PROVISION_PAT (preferred) or "
                "GITHUB_PAT / GITHUB_TOKEN via Doppler (motto-core/prd)."
            )
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "motto-mcp-server",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        resp = await self._client.request(method, path, params=params, json=json_body)
        return _handle_response(resp, method=method, path=path)


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


def _handle_response(resp: httpx.Response, *, method: str, path: str) -> Any:
    if resp.status_code == 204 or not resp.content:
        if resp.status_code >= 400:
            _raise_http_error(resp, method=method, path=path)
        return {}
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    if resp.status_code >= 400:
        _raise_http_error(resp, method=method, path=path, parsed=data)
    return data


def _raise_http_error(
    resp: httpx.Response,
    *,
    method: str,
    path: str,
    parsed: Any = None,
) -> None:
    if parsed is None:
        try:
            parsed = resp.json()
        except ValueError:
            parsed = resp.text
    summary = str(parsed)
    if len(summary) > 400:
        summary = summary[:397] + "..."
    raise RuntimeError(f"GitHub {method} {path} failed [{resp.status_code}]: {summary}")


# ---------------------------------------------------------------------------
# Lazy client holder — lets tests inject a fake without touching env
# ---------------------------------------------------------------------------


_client_holder: dict[str, GitHubClient] = {}


def _client() -> GitHubClient:
    if "c" not in _client_holder:
        _client_holder["c"] = GitHubClient()
    return _client_holder["c"]


def set_client(client: GitHubClient | None) -> None:
    """Swap the lazy client (tests). Pass None to reset."""
    if client is None:
        _client_holder.pop("c", None)
    else:
        _client_holder["c"] = client


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_repos(
    owner: str | None = None,
    affiliation: str = "owner,collaborator,organization_member",
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List repositories visible to the PAT.

    Args:
        owner: when set, lists repos for an org (``/orgs/{owner}/repos``);
            otherwise the authenticated user's repos (``/user/repos``).
        affiliation: ignored when ``owner`` is set; otherwise comma-separated
            GitHub affiliation filter.
        per_page: page size, 1-100.
    Returns: list of repo dicts.
    """
    if owner:
        path = f"/orgs/{owner}/repos"
        params: dict[str, Any] = {"per_page": per_page}
    else:
        path = "/user/repos"
        params = {"per_page": per_page, "affiliation": affiliation}
    data = await _client().request("GET", path, params=params)
    return data if isinstance(data, list) else []


@mcp.tool()
async def get_repo(owner: str, repo: str) -> dict[str, Any]:
    """Fetch a single repository.

    Args:
        owner: GitHub user or org.
        repo: repository name.
    Returns: repo dict.
    """
    data = await _client().request("GET", f"/repos/{owner}/{repo}")
    return data if isinstance(data, dict) else {}


@mcp.tool()
async def list_issues(
    owner: str,
    repo: str,
    state: str = "open",
    labels: str | None = None,
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List issues in a repository.

    Args:
        owner: repo owner.
        repo: repo name.
        state: ``open`` / ``closed`` / ``all``.
        labels: optional comma-separated label names.
        per_page: page size, 1-100.
    Returns: list of issue dicts (note: GitHub returns PRs here too; filter on
        ``pull_request`` key client-side if you need issues-only).
    """
    params: dict[str, Any] = {"state": state, "per_page": per_page}
    if labels:
        params["labels"] = labels
    data = await _client().request("GET", f"/repos/{owner}/{repo}/issues", params=params)
    return data if isinstance(data, list) else []


@mcp.tool()
async def list_pulls(
    owner: str,
    repo: str,
    state: str = "open",
    per_page: int = 30,
) -> list[dict[str, Any]]:
    """List pull requests in a repository.

    Args:
        owner: repo owner.
        repo: repo name.
        state: ``open`` / ``closed`` / ``all``.
        per_page: page size, 1-100.
    Returns: list of PR dicts.
    """
    params = {"state": state, "per_page": per_page}
    data = await _client().request("GET", f"/repos/{owner}/{repo}/pulls", params=params)
    return data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Write tools (each gated by confirm=True)
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create an issue. confirm=True REQUIRED.

    Args:
        owner: repo owner.
        repo: repo name.
        title: issue title.
        body: optional Markdown body.
        labels: optional list of label names to apply.
        assignees: optional list of GitHub logins to assign.
        confirm: must be True or the call is refused.
    Returns: the created issue dict.
    """
    if not confirm:
        _refuse("create_issue", owner=owner, repo=repo, title=title)
    payload: dict[str, Any] = {"title": title}
    if body is not None:
        payload["body"] = body
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    data = await _client().request(
        "POST", f"/repos/{owner}/{repo}/issues", json_body=payload
    )
    return data if isinstance(data, dict) else {}


@mcp.tool()
async def comment_issue(
    owner: str,
    repo: str,
    issue_number: int,
    body: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Post a comment on an issue or pull request. confirm=True REQUIRED.

    Args:
        owner: repo owner.
        repo: repo name.
        issue_number: issue or PR number.
        body: Markdown body.
        confirm: must be True or the call is refused.
    Returns: the created comment dict.
    """
    if not confirm:
        _refuse("comment_issue", owner=owner, repo=repo, issue_number=issue_number)
    data = await _client().request(
        "POST",
        f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        json_body={"body": body},
    )
    return data if isinstance(data, dict) else {}


@mcp.tool()
async def create_pull(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: str | None = None,
    draft: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Open a pull request. confirm=True REQUIRED.

    Args:
        owner: repo owner.
        repo: repo name.
        title: PR title.
        head: source branch (``owner:branch`` for cross-fork PRs).
        base: target branch (e.g. ``main``).
        body: optional Markdown body.
        draft: open as draft PR.
        confirm: must be True or the call is refused.
    Returns: the created PR dict.
    """
    if not confirm:
        _refuse("create_pull", owner=owner, repo=repo, head=head, base=base)
    payload: dict[str, Any] = {
        "title": title,
        "head": head,
        "base": base,
        "draft": draft,
    }
    if body is not None:
        payload["body"] = body
    data = await _client().request(
        "POST", f"/repos/{owner}/{repo}/pulls", json_body=payload
    )
    return data if isinstance(data, dict) else {}


@mcp.tool()
async def merge_pull(
    owner: str,
    repo: str,
    pull_number: int,
    merge_method: str = "squash",
    commit_title: str | None = None,
    commit_message: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Merge a pull request. confirm=True REQUIRED.

    Args:
        owner: repo owner.
        repo: repo name.
        pull_number: PR number.
        merge_method: ``merge`` / ``squash`` / ``rebase`` (default ``squash``).
        commit_title: optional commit title (squash/merge only).
        commit_message: optional commit message body.
        confirm: must be True or the call is refused.
    Returns: merge result dict (``merged``, ``sha``, ``message``).
    """
    if not confirm:
        _refuse("merge_pull", owner=owner, repo=repo, pull_number=pull_number)
    payload: dict[str, Any] = {"merge_method": merge_method}
    if commit_title:
        payload["commit_title"] = commit_title
    if commit_message:
        payload["commit_message"] = commit_message
    data = await _client().request(
        "PUT",
        f"/repos/{owner}/{repo}/pulls/{pull_number}/merge",
        json_body=payload,
    )
    return data if isinstance(data, dict) else {}


@mcp.tool()
async def set_secret(
    owner: str,
    repo: str,
    name: str,
    value: str,
    environment: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Set a repo or environment Actions secret using the fleet provisioner PAT.

    The plaintext is encrypted client-side with libsodium sealed-box against
    the repo's public key before transit. confirm=True REQUIRED.

    Args:
        owner: repo owner.
        repo: repo name.
        name: secret name (uppercase, snake_case).
        value: plaintext secret value.
        environment: optional environment name (sets an env-scoped secret instead
            of a repo-scoped one).
        confirm: must be True or the call is refused.
    Returns: ``{"set": True, "owner", "repo", "name", "scope"}``.
    """
    if not confirm:
        _refuse("set_secret", owner=owner, repo=repo, name=name)
    if environment:
        key_path = f"/repos/{owner}/{repo}/environments/{environment}/secrets/public-key"
        put_path = f"/repos/{owner}/{repo}/environments/{environment}/secrets/{name}"
        scope = f"environment:{environment}"
    else:
        key_path = f"/repos/{owner}/{repo}/actions/secrets/public-key"
        put_path = f"/repos/{owner}/{repo}/actions/secrets/{name}"
        scope = "repo"
    pubkey = await _client().request("GET", key_path)
    if not isinstance(pubkey, dict) or "key" not in pubkey or "key_id" not in pubkey:
        raise RuntimeError(f"set_secret: malformed public-key response: {pubkey}")
    encrypted = _seal(value, pubkey["key"])
    await _client().request(
        "PUT",
        put_path,
        json_body={"encrypted_value": encrypted, "key_id": pubkey["key_id"]},
    )
    return {"set": True, "owner": owner, "repo": repo, "name": name, "scope": scope}


def _seal(plaintext: str, public_key_b64: str) -> str:
    """Encrypt ``plaintext`` to ``public_key_b64`` using libsodium sealed-box.

    Imported lazily so the rest of the GitHub server (read tools, tests) works
    without PyNaCl installed.
    """
    try:
        from nacl.encoding import Base64Encoder
        from nacl.public import PublicKey, SealedBox
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "set_secret requires PyNaCl. Install via the 'github' extra "
            "(`pip install -e .[github]`) or `pip install pynacl`."
        ) from exc
    box = SealedBox(PublicKey(public_key_b64.encode("utf-8"), encoder=Base64Encoder))
    sealed = box.encrypt(plaintext.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _refuse(tool: str, **scope: Any) -> None:
    raise RuntimeError(
        f"{tool}: confirm=False; refusing to mutate. Re-call with confirm=True. scope={scope}"
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Run the GitHub MCP server. stdio by default; set MCP_TRANSPORT=http
    to expose over HTTP (PORT, default 8084)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8084"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
