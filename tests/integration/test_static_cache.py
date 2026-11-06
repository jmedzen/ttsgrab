"""The SPA shell must never be cached, and the hashed assets always must be.

This is the difference between shipping a feature and a user telling you it does not
exist: index.html names the content-hashed bundle, so a cached copy keeps pointing at the
previous build's filename and the browser never asks for the new code.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

# The SPA is a build artefact (`make web`) and is gitignored, so a fresh checkout — which is
# what CI runs — has no index.html. Asserting a cache header on a 503 fallback would be
# testing the placeholder page.
_HAS_SPA = (
    __import__("pathlib").Path(__file__).resolve().parents[2]
    / "src"
    / "ttsgrab"
    / "web"
    / "index.html"
).is_file()

needs_spa = pytest.mark.skipif(not _HAS_SPA, reason="SPA not built (run `make web`)")


async def test_the_placeholder_page_is_uncacheable_too(client):
    """When the SPA is not built the server explains how to build it.

    That placeholder must not be cached either — otherwise a user who runs `make web` and
    restarts still sees "the web UI has not been built yet" and concludes it failed.
    """
    if _HAS_SPA:
        pytest.skip("SPA is built; the placeholder is not served")
    r = await client.get("/")
    assert r.status_code == 200
    assert "make web" in r.text
    assert "no-cache" in r.headers.get("cache-control", "")


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@needs_spa
async def test_index_html_is_never_cached(client):
    r = await client.get("/")
    assert r.status_code == 200
    assert "no-cache" in r.headers.get("cache-control", "")


@needs_spa
async def test_index_html_carries_an_etag_and_revalidates_to_304(client):
    first = await client.get("/")
    etag = first.headers.get("etag")
    # No content hash in the filename, so revalidation is the only cheap path.
    assert etag, "index.html needs an ETag or `no-cache` costs a full download every load"

    again = await client.get("/", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


@needs_spa
async def test_index_reports_the_serving_build(client):
    r = await client.get("/")
    # Someone reporting "I still see the old UI" needs to be asked for this value.
    assert r.headers.get("x-ttsgrab-build")


@needs_spa
async def test_head_works_for_the_shell_and_liveness(client):
    """Uptime monitors and load balancers probe with HEAD; FastAPI omits it by default."""
    assert (await client.head("/")).status_code == 200
    assert (await client.head("/healthz")).status_code == 200


@needs_spa
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
