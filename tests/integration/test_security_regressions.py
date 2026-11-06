"""Regression tests for the audit findings that were actually exploitable.

Each test reproduces a specific published attack against the fixed code. They are named for
the attack, not the fix, so a future refactor that reopens one fails with a name that says
what it means.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def anon(app):
    """A client with no credentials, from a non-localhost address."""
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.9", 1234)),
        base_url="http://testserver",
    ) as c:
        yield c


@pytest.fixture
async def admin(app, settings):
    token = settings.security.admin_token or ""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"} if token else {},
    ) as c:
        yield c


# -- path traversal through the SPA catch-all -------------------------------------------


@pytest.mark.parametrize(
    "attack",
    [
        "/..%2f..%2f..%2f.env",
        "/..%2f..%2fconfig.yaml",
        "/..%2f..%2f..%2f..%2f..%2fetc%2fpasswd",
        "/%2e%2e%2f%2e%2e%2f.env",
        "/....//....//.env",
    ],
)
async def test_spa_route_cannot_escape_the_build_directory(anon, attack):
    """Uvicorn percent-decodes before routing, so `..%2f` is a real traversal by the time
    Starlette's `{path:path}` converter sees it. `is_file()` on the joined path happily read
    the operator's .env — including the admin token — with no credentials at all."""
    r = await anon.get(attack)
    body = r.text
    for marker in ("ADMIN_TOKEN", "root:x:", "Secrets belong", "SQLite format"):
        assert marker not in body, f"{attack} leaked {marker!r}"


@pytest.fixture
def no_spa(monkeypatch, tmp_path):
    """Force the "SPA not built" branch.

    The SPA is a build artefact and gitignored, so a fresh checkout — what CI runs — has no
    index.html and serves the placeholder. Locally it exists, so a test that does not force
    one branch or the other passes for a different reason here than in CI. That is how the
    CI failure that prompted this test got in.
    """
    from ttsgrab import main as main_mod

    empty = tmp_path / "web"
    empty.mkdir()
    monkeypatch.setattr(main_mod, "WEB_DIST", empty)
    return empty


async def test_placeholder_answers_deep_links_not_just_the_root(no_spa, anon):
    """A bookmarked /settings on a source checkout must not 404.

    The placeholder exists to say "run `make web`". Only registering it on `/` meant the one
    user who needed that message — someone with a bookmark — got a bare 404 instead.
    """
    for path in ("/", "/settings", "/history", "/anything/deep"):
        r = await anon.get(path)
        assert r.status_code == 200, f"{path} returned {r.status_code} instead of the placeholder"
        assert "make web" in r.text


async def test_placeholder_never_answers_an_api_path(no_spa, anon):
    """HTML for `/v1/*` would be a far worse bug than a 404: clients parse it."""
    r = await anon.get("/v1/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


async def test_the_traversal_block_holds_without_a_built_spa(no_spa, anon):
    """The other catch-all needs the same confinement, or the placeholder branch is the way in."""
    r = await anon.get("/..%2f..%2f..%2f.env")
    assert "ADMIN_TOKEN" not in r.text
    assert "Secrets belong" not in r.text


async def test_an_api_path_is_never_answered_with_html(anon):
    """Whatever the SPA branch does, it must not swallow `/v1/*`."""
    r = await anon.get("/v1/does-not-exist")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")


# -- /v1/settings -----------------------------------------------------------------------


async def test_settings_requires_admin_not_merely_a_valid_key(anon, admin):
    assert (await anon.get("/v1/settings")).status_code in (401, 403)


async def test_settings_never_returns_an_unmasked_secret(admin):
    """`mask_tree` matched only nested keys, but `/v1/settings` builds dotted ones, so
    `security.admin_token` came back in plaintext."""
    r = await admin.get("/v1/settings")
    assert r.status_code == 200
    body = r.text
    # Whatever the configured token is, it must not appear verbatim.
    for key in ("api_key", "admin_token", "free_api_keys", "paid_api_keys"):
        value = r.json()["values"].get(key) or r.json()["values"].get(f"security.{key}")
        if isinstance(value, str) and len(value) > 8:
            assert value not in body or "•" in value


# -- provider allowlist -----------------------------------------------------------------


async def test_a_restricted_key_must_name_its_provider(app, settings):
    """Omitting `provider` used to skip the allowlist entirely and let the router pick —
    so an edge-only key could be billed against Google."""
    from tests.helpers_keys import make_key

    token = await make_key(app, settings, allowlist=["edge"])
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r = await c.post("/v1/tts", json={"text": "hi", "voice": "en-US-AriaNeural"})
        assert r.status_code in (400, 403)
        assert r.json()["error"]["code"] in ("invalid_request", "forbidden")

        # And naming a provider it may not use is still refused.
        r2 = await c.post(
            "/v1/tts",
            json={"text": "hi", "voice": "en-US-AriaNeural", "provider": "google_cloud"},
        )
        assert r2.status_code == 403


# -- history isolation ------------------------------------------------------------------


async def test_history_is_scoped_to_the_calling_key(app, settings):
    """`api_key_id` was recorded on every row and never filtered on."""
    from tests.helpers_keys import make_key

    key_a = await make_key(app, settings, name="tenant-a")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {key_a}"},
    ) as a:
        listed = await a.get("/v1/history")
        assert listed.status_code == 200
        ids = [h["id"] for h in listed.json()["items"]]
        # Whatever it can list, it must be able to fetch only those.
        for rid in ids:
            assert (await a.get(f"/v1/history/{rid}/audio")).status_code in (200, 404)

    # A different key must not see them at all.
    key_b = await make_key(app, settings, name="tenant-b")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {key_b}"},
    ) as b:
        foreign = [h["id"] for h in (await b.get("/v1/history")).json()["items"]]
        for rid in ids:
            assert rid not in foreign
