"""Integration tests for motto-mcp-server core MCP tools and endpoints.

Tests exercise the MCP server's tool registration, health endpoints,
and fleet coordination tools using mocked database connections.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


class TestMCPImports:
    """Smoke tests: all core modules import without errors."""

    def test_server_imports(self) -> None:
        """mcp_server.server is importable."""
        import mcp_server.server

        assert mcp_server.server is not None

    def test_db_imports(self) -> None:
        """mcp_server.db is importable."""
        import mcp_server.db

        assert mcp_server.db is not None

    def test_auth_imports(self) -> None:
        """mcp_server.auth is importable."""
        import mcp_server.auth

        assert mcp_server.auth is not None

    def test_fleet_context_imports(self) -> None:
        """mcp_server.fleet_context is importable."""
        import mcp_server.fleet_context

        assert mcp_server.fleet_context is not None

    def test_handlers_imports(self) -> None:
        """mcp_server.handlers.deepseek is importable."""
        import mcp_server.handlers.deepseek

        assert mcp_server.handlers.deepseek is not None

    def test_routes_imports(self) -> None:
        """mcp_server.routes is importable."""
        import mcp_server.routes

        assert mcp_server.routes is not None

    def test_templates_imports(self) -> None:
        """mcp_server.templates is importable."""
        import mcp_server.templates

        assert mcp_server.templates is not None

    def test_verifiers_imports(self) -> None:
        """mcp_server.verifiers is importable."""
        import mcp_server.verifiers
        import mcp_server.verifiers.merge_pr
        import mcp_server.verifiers.noop
        import mcp_server.verifiers.types

        assert mcp_server.verifiers is not None


class TestHealthEndpoint:
    """Verify the health check endpoint is correctly defined."""

    def test_healthz_route_exists(self) -> None:
        """The /healthz custom route is registered on the MCP server."""
        from mcp_server.server import healthz

        assert healthz is not None
        assert callable(healthz)

    @pytest.mark.asyncio
    async def test_healthz_returns_ok(self) -> None:
        """healthz handler returns PlainTextResponse with 'ok'."""
        from unittest.mock import MagicMock

        from mcp_server.server import healthz

        request = MagicMock()
        response = await healthz(request)
        assert response.body == b"ok"
        assert response.status_code == 200


class TestServerAuth:
    """Verify auth middleware logic."""

    def test_auth_ok_no_token(self) -> None:
        """_auth_ok returns True when MOTTO_MCP_AUTH_TOKEN is unset."""
        with patch.dict(os.environ, {}, clear=True):
            from mcp_server.server import _auth_ok

            request = MagicMock()
            request.headers = {}
            request.query_params = {}
            assert _auth_ok(request) is True

    def test_auth_ok_valid_header(self) -> None:
        """_auth_ok returns True with correct Authorization header."""
        token = "test-token-12345"
        with patch.dict(os.environ, {"MOTTO_MCP_AUTH_TOKEN": token}, clear=True):
            from mcp_server.server import _auth_ok

            request = MagicMock()
            request.headers = {"authorization": f"Bearer {token}"}
            request.query_params = {}
            assert _auth_ok(request) is True

    def test_auth_ok_valid_query_param(self) -> None:
        """_auth_ok returns True with correct ?token= query param."""
        token = "test-token-12345"
        with patch.dict(os.environ, {"MOTTO_MCP_AUTH_TOKEN": token}, clear=True):
            from mcp_server.server import _auth_ok

            request = MagicMock()
            request.headers = {}
            request.query_params = {"token": token}
            assert _auth_ok(request) is True

    def test_auth_ok_invalid_token(self) -> None:
        """_auth_ok returns False with incorrect token."""
        with patch.dict(os.environ, {"MOTTO_MCP_AUTH_TOKEN": "correct"}, clear=True):
            from mcp_server.server import _auth_ok

            request = MagicMock()
            request.headers = {"authorization": "Bearer wrong"}
            request.query_params = {}
            assert _auth_ok(request) is False


class TestSentryIntegration:
    """Verify Sentry error tracking is wired correctly."""

    def test_init_sentry_returns_false_without_dsn(self) -> None:
        """init_sentry returns False when SENTRY_DSN is unset."""
        from motto_common.sentry_init import init_sentry

        result = init_sentry(agent_name="test-mcp-server")
        assert isinstance(result, bool)
        if not os.environ.get("SENTRY_DSN"):
            assert not result

    def test_default_host_is_northflank(self) -> None:
        """DEFAULT_HOST constant is 'northflank'."""
        from motto_common.sentry_init import DEFAULT_HOST

        assert DEFAULT_HOST == "northflank"

    def test_capture_main_loop_reraises(self) -> None:
        """capture_main_loop decorator re-raises exceptions."""
        from motto_common.sentry_init import capture_main_loop

        @capture_main_loop
        def faulty() -> None:
            raise RuntimeError("test mcp error")

        with pytest.raises(RuntimeError, match="test mcp error"):
            faulty()


class TestMCPToolRegistration:
    """Verify core MCP tools are registered.

    These tests check that the server module defines the key fleet
    coordination tools. They do not exercise the full MCP runtime.
    """

    def test_register_agent_tool_exists(self) -> None:
        """register_agent tool is defined."""
        from mcp_server.server import register_agent

        assert callable(register_agent)

    def test_heartbeat_tool_exists(self) -> None:
        """heartbeat tool is defined."""
        from mcp_server.server import heartbeat

        assert callable(heartbeat)

    def test_record_run_start_tool_exists(self) -> None:
        """record_run_start tool is defined."""
        from mcp_server.server import record_run_start

        assert callable(record_run_start)

    def test_record_run_end_tool_exists(self) -> None:
        """record_run_end tool is defined."""
        from mcp_server.server import record_run_end

        assert callable(record_run_end)

    def test_get_fleet_status_tool_exists(self) -> None:
        """get_fleet_status tool is defined."""
        from mcp_server.server import get_fleet_status

        assert callable(get_fleet_status)

    def test_get_recent_events_tool_exists(self) -> None:
        """get_recent_events tool is defined."""
        from mcp_server.server import get_recent_events

        assert callable(get_recent_events)

    def test_signal_intent_tool_exists(self) -> None:
        """signal_intent tool is defined."""
        from mcp_server.server import signal_intent

        assert callable(signal_intent)

    def test_verify_move_tool_exists(self) -> None:
        """verify_move tool is defined."""
        from mcp_server.server import verify_move

        assert callable(verify_move)

    def test_claim_next_step_tool_exists(self) -> None:
        """claim_next_step tool is defined."""
        from mcp_server.server import claim_next_step

        assert callable(claim_next_step)
