"""The SPA shell must never be cached, and the hashed assets always must be.

This is the difference between shipping a feature and a user telling you it does not
exist: index.html names the content-hashed bundle, so a cached copy keeps pointing at the
previous build's filename and the browser never asks for the new code.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


async def test_index_html_is_never_cached(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")


async def test_index_html_carries_an_etag_and_revalidates_to_304(client):
    first = await client.get("/")
    etag = first.headers.get("etag")
    # No content hash in the filename, so revalidation is the only cheap path.
    assert etag, "index.html needs an ETag or `no-cache` costs a full download every load"

    again = await client.get("/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


async def test_index_reports_the_serving_build(client):
    r = await client.get("/")
    # Someone reporting "I still see the old UI" needs to be asked for this value.
    assert r.headers.get("x-ttsgrab-build")


async def test_head_works_for_the_shell_and_liveness(client):
    """Uptime monitors and load balancers probe with HEAD; FastAPI omits it by default."""
    assert (await client.head("/")).status_code == 200
    assert (await client.head("/healthz")).status_code == 200


async def test_hashed_assets_are_cached_forever(client):
    """Vite puts a content hash in the filename, so a changed file gets a new name."""
    index = await client.get("/")
    import re

    m = re.search(r"assets/(index-[A-Za-z0-9_-]+\.js)", index.text)
    if not m:
        pytest.skip("SPA not built in this checkout")
    asset = await client.get(f"/assets/{m.group(1)}")
    assert asset.status_code == 200
    assert "immutable" in asset.headers.get("cache-control", "")
