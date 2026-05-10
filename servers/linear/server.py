"""Linear MCP server — wraps the Linear GraphQL API for the Motto fleet.

Exposes issue/project listing plus issue create/update and comment posting.
Read tools are unrestricted; every mutating tool requires ``confirm=True``.

Run with ``python -m servers.linear`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.

Auth: ``LINEAR_API_KEY`` from env (canonical home is Doppler
``motto-core/prd``). The key is read lazily so import-time / pytest
collection works without it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

LINEAR_API = "https://api.linear.app/graphql"
DEFAULT_TIMEOUT = 30.0

mcp = FastMCP(
    "motto-linear",
    instructions=(
        "Linear GraphQL API for the Motto fleet — issues, projects, and "
        "comments. Reads are unrestricted; every mutating tool (create_issue, "
        "update_issue, create_comment) requires confirm=true. "
        "Key read from LINEAR_API_KEY env (Doppler motto-core/prd)."
    ),
)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class LinearClient:
    """Thin async wrapper around Linear's GraphQL endpoint.

    Linear authenticates with a personal/api-key passed verbatim in the
    ``Authorization`` header (no ``Bearer`` prefix).
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("LINEAR_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "LINEAR_API_KEY is required. Set it via Doppler "
                "(motto-core/prd) and inject at runtime."
            )
        self._client = httpx.AsyncClient(
            base_url=LINEAR_API,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "Authorization": self._api_key,
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def query(
        self,
        graphql: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """POST a GraphQL query/mutation. Raises RuntimeError on transport
        failure or any ``errors`` array in the response."""
        resp = await self._client.post(
            "", json={"query": graphql, "variables": variables or {}}
        )
        if resp.status_code >= 400:
            _raise_http_error(resp)
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"Linear: non-JSON response: {resp.text[:400]}") from exc
        if data.get("errors"):
            errs = data["errors"]
            summary = "; ".join(
                e.get("message", str(e)) if isinstance(e, dict) else str(e)
                for e in errs
            )
            raise RuntimeError(f"Linear GraphQL error: {summary}")
        return data.get("data", {}) or {}


def _raise_http_error(resp: httpx.Response) -> None:
    try:
        parsed: Any = resp.json()
    except ValueError:
        parsed = resp.text
    summary = str(parsed)
    if len(summary) > 400:
        summary = summary[:397] + "..."
    raise RuntimeError(f"Linear HTTP {resp.status_code}: {summary}")


# ---------------------------------------------------------------------------
# Lazy client holder
# ---------------------------------------------------------------------------


_client_holder: dict[str, LinearClient] = {}


def _client() -> LinearClient:
    if "c" not in _client_holder:
        _client_holder["c"] = LinearClient()
    return _client_holder["c"]


def set_client(client: LinearClient | None) -> None:
    """Swap the lazy client (tests). Pass None to reset."""
    if client is None:
        _client_holder.pop("c", None)
    else:
        _client_holder["c"] = client


# ---------------------------------------------------------------------------
# GraphQL fragments / queries
# ---------------------------------------------------------------------------


_ISSUE_FIELDS = """
id identifier title state { id name type } priority
url createdAt updatedAt assignee { id name } team { id key name }
"""

_LIST_ISSUES_QUERY = (
    "query ListIssues($first: Int!, $filter: IssueFilter) {\n"
    "  issues(first: $first, filter: $filter) {\n"
    "    nodes { " + _ISSUE_FIELDS + " }\n"
    "  }\n"
    "}\n"
)

_GET_ISSUE_QUERY = (
    "query GetIssue($id: String!) {\n"
    "  issue(id: $id) {\n    " + _ISSUE_FIELDS +
    "    description\n"
    "  }\n"
    "}\n"
)

_LIST_PROJECTS_QUERY = """
query ListProjects($first: Int!) {
  projects(first: $first) {
    nodes { id name state url description targetDate }
  }
}
"""

_CREATE_ISSUE_MUTATION = """
mutation CreateIssue($input: IssueCreateInput!) {
  issueCreate(input: $input) {
    success
    issue { id identifier title url state { name } }
  }
}
"""

_UPDATE_ISSUE_MUTATION = """
mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { id identifier title url state { name } }
  }
}
"""

_CREATE_COMMENT_MUTATION = """
mutation CreateComment($input: CommentCreateInput!) {
  commentCreate(input: $input) {
    success
    comment { id body url createdAt }
  }
}
"""


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def list_issues(
    team_key: str | None = None,
    state_name: str | None = None,
    first: int = 25,
) -> list[dict[str, Any]]:
    """List Linear issues.

    Args:
        team_key: optional team key (e.g. ``MOT``); filters server-side.
        state_name: optional workflow-state name (``In Progress``, ``Done``...).
        first: page size, 1-100.
    Returns: list of issue dicts.
    """
    issue_filter: dict[str, Any] = {}
    if team_key:
        issue_filter["team"] = {"key": {"eq": team_key}}
    if state_name:
        issue_filter["state"] = {"name": {"eq": state_name}}
    data = await _client().query(
        _LIST_ISSUES_QUERY,
        variables={"first": first, "filter": issue_filter or None},
    )
    return ((data.get("issues") or {}).get("nodes") or [])


@mcp.tool()
async def get_issue(id: str) -> dict[str, Any]:
    """Fetch a single Linear issue.

    Args:
        id: issue UUID or identifier (e.g. ``MOT-15``).
    Returns: issue dict.
    """
    data = await _client().query(_GET_ISSUE_QUERY, variables={"id": id})
    return data.get("issue") or {}


@mcp.tool()
async def list_projects(first: int = 25) -> list[dict[str, Any]]:
    """List Linear projects visible to the API key.

    Args:
        first: page size, 1-100.
    Returns: list of project dicts.
    """
    data = await _client().query(_LIST_PROJECTS_QUERY, variables={"first": first})
    return ((data.get("projects") or {}).get("nodes") or [])


# ---------------------------------------------------------------------------
# Write tools (each gated by confirm=True)
# ---------------------------------------------------------------------------


@mcp.tool()
async def create_issue(
    team_id: str,
    title: str,
    description: str | None = None,
    priority: int | None = None,
    assignee_id: str | None = None,
    project_id: str | None = None,
    label_ids: list[str] | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Create a Linear issue. confirm=True REQUIRED.

    Args:
        team_id: Linear team UUID (use ``list_projects`` / Linear UI to find).
        title: issue title.
        description: optional Markdown body.
        priority: 0=No 1=Urgent 2=High 3=Medium 4=Low.
        assignee_id: optional user UUID.
        project_id: optional project UUID.
        label_ids: optional list of label UUIDs.
        confirm: must be True or the call is refused.
    Returns: ``{"success": bool, "issue": {...}}``.
    """
    if not confirm:
        _refuse("create_issue", team_id=team_id, title=title)
    issue_input: dict[str, Any] = {"teamId": team_id, "title": title}
    if description is not None:
        issue_input["description"] = description
    if priority is not None:
        issue_input["priority"] = priority
    if assignee_id:
        issue_input["assigneeId"] = assignee_id
    if project_id:
        issue_input["projectId"] = project_id
    if label_ids:
        issue_input["labelIds"] = label_ids
    data = await _client().query(_CREATE_ISSUE_MUTATION, variables={"input": issue_input})
    return data.get("issueCreate") or {}


@mcp.tool()
async def update_issue(
    id: str,
    title: str | None = None,
    description: str | None = None,
    state_id: str | None = None,
    assignee_id: str | None = None,
    priority: int | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Update a Linear issue. confirm=True REQUIRED.

    Args:
        id: issue UUID or identifier (``MOT-15``).
        title: new title (omit to keep).
        description: new Markdown body (omit to keep).
        state_id: new workflow-state UUID (omit to keep).
        assignee_id: new assignee UUID (omit to keep).
        priority: new priority (omit to keep).
        confirm: must be True or the call is refused.
    Returns: ``{"success": bool, "issue": {...}}``.
    """
    if not confirm:
        _refuse("update_issue", id=id)
    update_input: dict[str, Any] = {}
    if title is not None:
        update_input["title"] = title
    if description is not None:
        update_input["description"] = description
    if state_id:
        update_input["stateId"] = state_id
    if assignee_id:
        update_input["assigneeId"] = assignee_id
    if priority is not None:
        update_input["priority"] = priority
    if not update_input:
        return {"success": False, "reason": "no fields to update"}
    data = await _client().query(
        _UPDATE_ISSUE_MUTATION, variables={"id": id, "input": update_input}
    )
    return data.get("issueUpdate") or {}


@mcp.tool()
async def create_comment(
    issue_id: str,
    body: str,
    confirm: bool = False,
) -> dict[str, Any]:
    """Post a comment on a Linear issue. confirm=True REQUIRED.

    Args:
        issue_id: issue UUID or identifier (``MOT-15``).
        body: Markdown body.
        confirm: must be True or the call is refused.
    Returns: ``{"success": bool, "comment": {...}}``.
    """
    if not confirm:
        _refuse("create_comment", issue_id=issue_id)
    data = await _client().query(
        _CREATE_COMMENT_MUTATION,
        variables={"input": {"issueId": issue_id, "body": body}},
    )
    return data.get("commentCreate") or {}


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
    """Run the Linear MCP server. stdio by default; set MCP_TRANSPORT=http
    to expose over HTTP (PORT, default 8085)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8085"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
