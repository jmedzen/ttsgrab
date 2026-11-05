"""Authorisation and SSRF.

These exist because the holes they cover were all live at once: `AdminDep` was defined and
never wired to a route, so any scoped API key could mint more keys; and
X-Forwarded-For was believed unconditionally, so a remote caller could claim to be
localhost and inherit the admin rights that implies.
"""

from __future__ import annotations

import pytest

from ttsgrab.core.security import generate_api_key


async def _make_key(name: str = "restricted", **overrides) -> str:
    from ttsgrab.db.models import ApiKey
    from ttsgrab.db.session import session_scope

    raw, digest, prefix = generate_api_key()
    async with session_scope() as session:
        session.add(ApiKey(key_hash=digest, name=name, prefix=prefix, **overrides))
    return raw


ADMIN_ROUTES = [
    ("GET", "/v1/keys"),
    ("POST", "/v1/keys"),
    ("GET", "/v1/audit"),
    ("GET", "/v1/workers"),
    ("GET", "/v1/references"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), ADMIN_ROUTES)
async def test_a_scoped_api_key_cannot_reach_admin_routes(client, ctx, method, path) -> None:
    """A key with a provider allowlist is a *limited* credential; it must not administer."""
    ctx.settings.security.admin_token = "admin-token"
    raw = await _make_key(provider_allowlist=["edge"], monthly_char_quota=1000)

    headers = {"Authorization": f"Bearer {raw}"}
    if method == "GET":
        r = await client.get(path, headers=headers)
    else:
        r = await client.post(path, json={"name": "escalated"}, headers=headers)

    assert r.status_code in (401, 403), f"{path} let a scoped key through: {r.text}"


@pytest.mark.asyncio
async def test_admin_token_reaches_admin_routes(client, ctx) -> None:
    """The fix must not break the legitimate caller."""
    ctx.settings.security.admin_token = "admin-token"
    r = await client.get("/v1/keys", headers={"Authorization": "Bearer admin-token"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_forwarded_for_is_ignored_by_default(client, ctx) -> None:
    """A client controls X-Forwarded-For, so it must not decide who is local."""
    ctx.settings.security.admin_token = "admin-token"
    ctx.settings.security.allow_localhost_without_token = True
    ctx.settings.security.trusted_proxy_hops = 0

    r = await client.get("/v1/keys", headers={"X-Forwarded-For": "127.0.0.1"})
    assert r.status_code in (401, 403), (
        "spoofing X-Forwarded-For granted admin — the header must be ignored unless "
        "security.trusted_proxy_hops says a proxy is present"
    )


@pytest.mark.asyncio
async def test_forwarded_for_is_honoured_only_as_configured(client, ctx) -> None:
    """With one trusted proxy, the rightmost entry is used and a client-prepended
    '127.0.0.1' is ignored."""
    from ttsgrab.api.deps import _client_ip

    class FakeRequest:
        def __init__(self, forwarded: str | None, peer: str = "203.0.113.9"):
            self.headers = {"x-forwarded-for": forwarded} if forwarded else {}
            self.client = type("C", (), {"host": peer})()

    # No proxy configured: always the socket peer.
    assert _client_ip(FakeRequest("127.0.0.1"), 0) == "203.0.113.9"

    # One proxy: the rightmost entry is the proxy's word; the spoof is discarded.
    spoofed = FakeRequest("127.0.0.1, 198.51.100.7")
    assert _client_ip(spoofed, 1) == "198.51.100.7"

    # Two proxies: walk two from the right.
    two = FakeRequest("1.2.3.4, 10.0.0.1, 198.51.100.7")
    assert _client_ip(two, 2) == "10.0.0.1"

    # More hops configured than provided must not crash or IndexError.
    assert _client_ip(FakeRequest("198.51.100.7"), 5) == "198.51.100.7"


@pytest.mark.asyncio
async def test_api_key_in_query_string_only_for_stream_paths(client, ctx) -> None:
    """Keys in URLs leak through logs and Referer; only the streaming path needs them."""
    ctx.settings.security.admin_token = "admin-token"
    r = await client.get("/v1/voices?api_key=admin-token")
    assert r.status_code in (401, 403), "query-string auth worked on a non-stream path"

    r = await client.get("/v1/keys?api_key=admin-token")
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_revoked_key_is_rejected(client, ctx) -> None:
    from ttsgrab.db.models import ApiKey
    from ttsgrab.db.session import session_scope

    ctx.settings.security.admin_token = "admin-token"
    raw, digest, prefix = generate_api_key()
    async with session_scope() as session:
        session.add(ApiKey(key_hash=digest, name="gone", prefix=prefix, revoked=True))
    r = await client.get("/v1/voices", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code in (401, 403)


@pytest.mark.asyncio
async def test_wrong_admin_token_is_rejected(client, ctx) -> None:
    ctx.settings.security.admin_token = "admin-token"
    r = await client.get("/v1/keys", headers={"Authorization": "Bearer admin-tokeX"})
    assert r.status_code in (401, 403)
