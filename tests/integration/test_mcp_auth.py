"""The MCP mount must not serve tools to an unauthenticated caller.

Its tools synthesise speech, which spends the operator's provider budget and bypasses every
per-key control — rate limit, provider allowlist, monthly quota — because the caller simply
has no key. The mount had no gate at all: `POST /mcp/` answered `initialize` with 200 and no
credentials.

Two layers are tested because they fail differently: the mount is refused when no token
exists (main.py), and requests are rejected when a token exists but is not presented
(mcp_server.py). Only the second one is exercised with a real token configured.
"""

from __future__ import annotations

import pytest

pytest.importorskip("mcp", reason="MCP extra is optional")

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream"}


async def test_mcp_refuses_to_mount_without_any_token(settings):
    """A misconfigured gateway must not expose a money-spending endpoint by default."""
    from ttsgrab.main import create_app

    settings.mcp.enabled = True
    settings.mcp.token = None
    settings.security.admin_token = None

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        # No mount means the path is not routed at all.
        assert getattr(app.state, "mcp_inner", None) is None


async def test_mcp_rejects_requests_without_the_bearer_token(settings):
    from httpx import ASGITransport, AsyncClient

    from ttsgrab.main import create_app

    settings.mcp.enabled = True
    settings.mcp.token = "mcp-secret-token-value"
    settings.security.admin_token = "admin-secret-token-value"

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        if getattr(app.state, "mcp_inner", None) is None:
            pytest.skip("MCP SDK could not be mounted in this environment")

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            anon = await c.post("/mcp/", json=INITIALIZE, headers=HEADERS)
            assert anon.status_code == 401, "the MCP transport answered without a token"

            wrong = await c.post(
                "/mcp/", json=INITIALIZE, headers={**HEADERS, "Authorization": "Bearer nope"}
            )
            assert wrong.status_code == 401

            # A malformed header must not be read as "no auth required".
            empty = await c.post(
                "/mcp/", json=INITIALIZE, headers={**HEADERS, "Authorization": "Bearer "}
            )
            assert empty.status_code == 401
