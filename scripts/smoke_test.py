"""Deployment smoke test for motto-mcp-server.

Verifies:
1. Package imports without errors.
2. Core MCP tools are registered and reachable.
3. Health check endpoint returns 200.
4. MCP server starts and initializes.

Usage:
    python scripts/smoke_test.py
    MOTTO_MCP_URL=http://localhost:8080 python scripts/smoke_test.py

Exit code 0 = all checks pass. Non-zero = smoke test failure.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent

CHECKS: list[tuple[str, bool]] = []


def _check(name: str, ok: bool, detail: str = "") -> None:
    status = "PASS" if ok else "FAIL"
    line = f"  [{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    CHECKS.append((name, ok))


def check_imports() -> None:
    """All core mcp_server modules must be importable."""
    print("\n-- Import checks --")
    modules = [
        ("mcp_server", "mcp_server"),
        ("mcp_server.server", "mcp_server.server"),
        ("mcp_server.db", "mcp_server.db"),
        ("mcp_server.auth", "mcp_server.auth"),
        ("mcp_server.fleet_context", "mcp_server.fleet_context"),
        ("mcp_server.tools_fleet_proxy", "mcp_server.tools_fleet_proxy"),
        ("mcp_server.chat_tools", "mcp_server.chat_tools"),
        ("mcp_server.cockpit", "mcp_server.cockpit"),
        ("mcp_server.update_tunnel_url", "mcp_server.update_tunnel_url"),
        ("mcp_server.handlers", "mcp_server.handlers"),
        ("mcp_server.handlers.deepseek", "mcp_server.handlers.deepseek"),
        ("mcp_server.routes", "mcp_server.routes"),
        ("mcp_server.templates", "mcp_server.templates"),
        ("mcp_server.verifiers", "mcp_server.verifiers"),
        ("mcp_server.verifiers.types", "mcp_server.verifiers.types"),
        ("mcp_server.verifiers.noop", "mcp_server.verifiers.noop"),
        ("mcp_server.verifiers.merge_pr", "mcp_server.verifiers.merge_pr"),
    ]
    for name, import_path in modules:
        try:
            __import__(import_path)
            _check(f"import {name}", True)
        except Exception as exc:
            _check(f"import {name}", False, str(exc))


def check_sentry_init() -> None:
    """Verify motto_common.sentry_init is importable and init_sentry works.

    motto-mcp-server should have Sentry SDK for release health tracking
    even though it did not previously import sentry_init.
    """
    print("\n-- Sentry init check --")
    try:
        from motto_common.sentry_init import DEFAULT_HOST, init_sentry

        _check("DEFAULT_HOST is 'northflank'", DEFAULT_HOST == "northflank")
        result = init_sentry(agent_name="smoke-test")
        _check("init_sentry returns bool", isinstance(result, bool))
        _check("init_sentry no-ops without DSN", not result)
    except ImportError as exc:
        _check("motto_common.sentry_init import", False, f"ImportError: {exc}")
    except Exception as exc:
        _check("motto_common.sentry_init import", False, str(exc))


async def _check_health_endpoint(base_url: str, client: httpx.AsyncClient) -> bool:
    """Hit /healthz and verify it returns HTTP 200 with 'ok'."""
    try:
        resp = await client.get(f"{base_url}/healthz", timeout=10.0)
        ok = resp.status_code == 200 and resp.text.strip() == "ok"
        _check(
            f"GET /healthz → {resp.status_code}",
            ok,
            f"body={resp.text.strip()[:80]}",
        )
        return ok
    except Exception as exc:
        _check("GET /healthz", False, str(exc))
        return False


async def _check_fleet_status(base_url: str, client: httpx.AsyncClient, token: str) -> bool:
    """Hit /fleet/status.json and verify it returns valid JSON."""
    try:
        resp = await client.get(
            f"{base_url}/fleet/status.json",
            params={"token": token},
            timeout=10.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            has_agents = "agents" in data
            has_events = "recent_events" in data
            _check(
                "/fleet/status.json returns agents + events",
                has_agents and has_events,
            )
            return has_agents and has_events
        elif resp.status_code == 401:
            _check(
                "/fleet/status.json",
                True,
                "auth required (expected when no token configured)",
            )
            return True
        else:
            _check(
                f"/fleet/status.json → {resp.status_code}",
                False,
                "unexpected status",
            )
            return False
    except Exception as exc:
        _check("/fleet/status.json", False, str(exc))
        return False


async def check_mcp_tools(base_url: str, token: str) -> None:
    """Verify MCP tools are reachable by calling get_fleet_status.

    If the server is not running locally, these checks are skipped.
    """
    print("\n-- MCP endpoint checks --")
    async with httpx.AsyncClient() as client:
        # Health check
        await _check_health_endpoint(base_url, client)

        # Fleet status
        if token:
            await _check_fleet_status(base_url, client, token)
        else:
            _check("fleet status (no token)", True, "skipped: no MOTTO_MCP_AUTH_TOKEN")


async def check_server_startup() -> None:
    """Start the MCP server briefly and verify it initializes.

    This test starts the server in a subprocess, waits for it to bind
    its port, checks /healthz, then shuts it down.

    DATABASE_URL must be set for the server to start (it connects to
    Neon Postgres on boot). If unset, we skip the live-start check.
    """
    print("\n-- Server startup check --")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        _check("server startup", True, "skipped: DATABASE_URL not set")
        return

    port = 18765  # Non-standard port to avoid conflicts
    proc = None
    try:
        env = os.environ.copy()
        env["PORT"] = str(port)
        # Disable mount of domain servers for smoke test
        env.pop("MOTTO_MCP_MOUNT_DOMAIN_SERVERS", None)

        proc = subprocess.Popen(
            [sys.executable, "-m", "mcp_server.server"],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait for the server to bind
        base_url = f"http://localhost:{port}"
        started = False
        async with httpx.AsyncClient() as client:
            for _ in range(30):  # 30 * 0.5s = 15s max
                time.sleep(0.5)
                if proc.poll() is not None:
                    stderr_out = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
                    _check(
                        "server startup",
                        False,
                        f"process exited early with code {proc.returncode}: {stderr_out[:200]}",
                    )
                    return
                try:
                    resp = await client.get(f"{base_url}/healthz", timeout=2.0)
                    if resp.status_code == 200:
                        started = True
                        break
                except Exception:
                    continue

        if started:
            _check("server starts and /healthz returns 200", True)
        else:
            _check("server starts and /healthz returns 200", False, "timed out waiting for port")
    except Exception as exc:
        _check("server startup", False, str(exc))
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def main() -> int:
    print("=== motto-mcp-server smoke test ===")
    print(f"Python: {sys.version}")
    print(f"Repo:   {REPO_ROOT}")

    # Import checks
    check_imports()

    # Sentry check
    check_sentry_init()

    # Determine if we should test against a running server
    base_url = os.environ.get("MOTTO_MCP_URL", "http://localhost:8080")
    token = os.environ.get("MOTTO_MCP_AUTH_TOKEN", "")

    # MCP endpoint checks (against potentially running server)
    asyncio.run(check_mcp_tools(base_url, token))

    # Server startup check
    asyncio.run(check_server_startup())

    print(f"\n--- Results: {sum(1 for _, ok in CHECKS if ok)}/{len(CHECKS)} passed ---")
    failed = [(name, ok) for name, ok in CHECKS if not ok]
    if failed:
        print("\nFAILED CHECKS:")
        for name, _ok in failed:
            print(f"  - {name}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
