"""Apollo MCP server — wraps the Apollo.io REST API for the Motto fleet.

Exposes people search, person enrichment, sequence listing, and
add-to-sequence. Read tools are unrestricted; ``add_to_sequence``
requires ``confirm=True`` because it spends Apollo credits and may
trigger outbound mail.

Run with ``python -m servers.apollo`` (stdio for Claude Code) or set
``MCP_TRANSPORT=http`` for HTTP inside the cluster.

Auth: ``APOLLO_API_KEY`` from env (canonical home is Doppler
``motto-core/prd``). Read lazily so import-time / pytest collection
work without it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastmcp import FastMCP

logger = logging.getLogger(__name__)

APOLLO_API = "https://api.apollo.io/v1"
DEFAULT_TIMEOUT = 30.0

mcp = FastMCP(
    "motto-apollo",
    instructions=(
        "Apollo.io API for the Motto fleet — people search, enrichment, "
        "and sequence operations. Reads are unrestricted; add_to_sequence "
        "requires confirm=true because it spends credits and may send mail. "
        "Key read from APOLLO_API_KEY env (Doppler motto-core/prd)."
    ),
)


# ---------------------------------------------------------------------------
# HTTP client
# ---------------------------------------------------------------------------


class ApolloClient:
    """Thin async wrapper around the Apollo REST API.

    Apollo authenticates via the ``X-Api-Key`` header *or* an ``api_key``
    body/query field. We use the header form for everything.
    """

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("APOLLO_API_KEY")
        if not self._api_key:
            raise RuntimeError(
                "APOLLO_API_KEY is required. Set it via Doppler "
                "(motto-core/prd) and inject at runtime."
            )
        self._client = httpx.AsyncClient(
            base_url=APOLLO_API,
            timeout=DEFAULT_TIMEOUT,
            headers={
                "X-Api-Key": self._api_key,
                "Cache-Control": "no-cache",
                "Accept": "application/json",
                "Content-Type": "application/json",
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
    raise RuntimeError(f"Apollo {method} {path} failed [{resp.status_code}]: {summary}")


# ---------------------------------------------------------------------------
# Lazy client holder
# ---------------------------------------------------------------------------


_client_holder: dict[str, ApolloClient] = {}


def _client() -> ApolloClient:
    if "c" not in _client_holder:
        _client_holder["c"] = ApolloClient()
    return _client_holder["c"]


def set_client(client: ApolloClient | None) -> None:
    """Swap the lazy client (tests). Pass None to reset."""
    if client is None:
        _client_holder.pop("c", None)
    else:
        _client_holder["c"] = client


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search_people(
    q_keywords: str | None = None,
    person_titles: list[str] | None = None,
    organization_locations: list[str] | None = None,
    organization_num_employees_ranges: list[str] | None = None,
    page: int = 1,
    per_page: int = 25,
) -> dict[str, Any]:
    """Search Apollo's people database (mixed-search endpoint).

    Args:
        q_keywords: free-text search across the person record.
        person_titles: e.g. ``["Director of Appraisal", "Chief Appraiser"]``.
        organization_locations: e.g. ``["Texas, US", "California, US"]``.
        organization_num_employees_ranges: e.g. ``["1,10", "11,50"]``.
        page: 1-based page number.
        per_page: 1-100.
    Returns: full Apollo response (``people``, ``pagination``, ...).
    """
    body: dict[str, Any] = {"page": page, "per_page": per_page}
    if q_keywords:
        body["q_keywords"] = q_keywords
    if person_titles:
        body["person_titles"] = person_titles
    if organization_locations:
        body["organization_locations"] = organization_locations
    if organization_num_employees_ranges:
        body["organization_num_employees_ranges"] = organization_num_employees_ranges
    data = await _client().request("POST", "/mixed_people/search", json_body=body)
    return data if isinstance(data, dict) else {"raw": data}


@mcp.tool()
async def enrich_person(
    email: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    organization_name: str | None = None,
    domain: str | None = None,
    linkedin_url: str | None = None,
    reveal_personal_emails: bool = False,
) -> dict[str, Any]:
    """Enrich a single person via Apollo's match endpoint.

    Provide whatever identifiers you have; Apollo uses them for fuzzy match.
    Returns ``{"person": {...}}`` on success.

    Args:
        email: best signal when available.
        first_name / last_name: name pair.
        organization_name: company name.
        domain: company domain.
        linkedin_url: LinkedIn profile URL.
        reveal_personal_emails: include personal email when matched (Apollo flag).
    """
    body: dict[str, Any] = {"reveal_personal_emails": reveal_personal_emails}
    if email:
        body["email"] = email
    if first_name:
        body["first_name"] = first_name
    if last_name:
        body["last_name"] = last_name
    if organization_name:
        body["organization_name"] = organization_name
    if domain:
        body["domain"] = domain
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    data = await _client().request("POST", "/people/match", json_body=body)
    return data if isinstance(data, dict) else {"raw": data}


@mcp.tool()
async def list_sequences(page: int = 1, per_page: int = 25) -> dict[str, Any]:
    """List Apollo email sequences (cadences).

    Args:
        page: 1-based page number.
        per_page: 1-100.
    Returns: ``{"emailer_campaigns": [...], "pagination": {...}}``.
    """
    data = await _client().request(
        "GET",
        "/emailer_campaigns/search",
        params={"page": page, "per_page": per_page},
    )
    return data if isinstance(data, dict) else {"raw": data}


# ---------------------------------------------------------------------------
# Write tools (gated by confirm=True)
# ---------------------------------------------------------------------------


@mcp.tool()
async def add_to_sequence(
    sequence_id: str,
    contact_ids: list[str],
    send_email_from_email_account_id: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Add Apollo contacts to a sequence. confirm=True REQUIRED.

    Args:
        sequence_id: emailer-campaign id.
        contact_ids: list of Apollo contact ids to enrol.
        send_email_from_email_account_id: optional email-account id; when set,
            the sequence sends from this account.
        confirm: must be True or the call is refused.
    Returns: Apollo's ``add_contact_ids_to_emailer_campaign`` response.
    """
    if not confirm:
        _refuse(
            "add_to_sequence",
            sequence_id=sequence_id,
            contact_count=len(contact_ids),
        )
    body: dict[str, Any] = {
        "emailer_campaign_id": sequence_id,
        "contact_ids": contact_ids,
    }
    if send_email_from_email_account_id:
        body["send_email_from_email_account_id"] = send_email_from_email_account_id
    data = await _client().request(
        "POST",
        f"/emailer_campaigns/{sequence_id}/add_contact_ids",
        json_body=body,
    )
    return data if isinstance(data, dict) else {"raw": data}


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
    """Run the Apollo MCP server. stdio by default; set MCP_TRANSPORT=http
    to expose over HTTP (PORT, default 8086)."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "http":
        port = int(os.environ.get("PORT", "8086"))
        mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
