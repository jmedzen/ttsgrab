"""Favourites live on the server, not in the browser.

The product is one person or a small team on a single instance. Favourites in localStorage
are tied to one browser, so the laptop cannot see what the phone starred — which is the
opposite of what a favourite is for.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def client(app, settings):
    token = settings.security.admin_token or ""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver", headers=headers
    ) as c:
        yield c


async def test_starring_a_voice_is_visible_to_a_second_client(client, settings):
    """The whole point: another browser reads the same shortlist."""
    await client.delete("/v1/favourites/zh-TW-HsiaoChenNeural")
    r = await client.post(
        "/v1/favourites",
        json={"voice_id": "zh-TW-HsiaoChenNeural", "provider": "edge", "name": "HsiaoChen"},
    )
    assert r.status_code == 200
    assert r.json()["created"] is True

    # A *different* client, no shared browser storage involved.
    async with AsyncClient(
        transport=ASGITransport(app=client._transport.app), base_url="http://testserver"
    ) as other:
        listing = await other.get("/v1/favourites")
        assert listing.status_code == 200
        ids = [f["voice_id"] for f in listing.json()["favourites"]]
        assert "zh-TW-HsiaoChenNeural" in ids


async def test_starring_twice_does_not_duplicate(client):
    await client.delete("/v1/favourites/dupe-test")
    first = await client.post("/v1/favourites", json={"voice_id": "dupe-test"})
    second = await client.post("/v1/favourites", json={"voice_id": "dupe-test"})
    assert first.json()["created"] is True
    assert second.json()["created"] is False
    ids = [f["voice_id"] for f in (await client.get("/v1/favourites")).json()["favourites"]]
    assert ids.count("dupe-test") == 1


async def test_a_disabled_providers_voice_still_has_a_readable_name(client):
    """The name is denormalised on purpose.

    A favourite whose provider is turned off drops out of /v1/voices. Without the stored
    name the UI would show a bare voice id, which reads as corruption.
    """
    await client.delete("/v1/favourites/ghost-voice")
    await client.post(
        "/v1/favourites",
        json={"voice_id": "ghost-voice", "provider": "gone", "name": "Ghost Voice"},
    )
    rows = (await client.get("/v1/favourites")).json()["favourites"]
    ghost = next(f for f in rows if f["voice_id"] == "ghost-voice")
    assert ghost["name"] == "Ghost Voice"
    assert ghost["provider"] == "gone"


async def test_unstarring_is_idempotent(client):
    await client.post("/v1/favourites", json={"voice_id": "remove-me"})
    assert (await client.delete("/v1/favourites/remove-me")).json()["removed"] is True
    # The UI removes optimistically and a second tab may have beaten us to it.
    assert (await client.delete("/v1/favourites/remove-me")).json()["removed"] is False


async def test_the_shortlist_has_a_cap(client):
    existing = (await client.get("/v1/favourites")).json()["favourites"]
    for row in existing:
        await client.delete(f"/v1/favourites/{row['voice_id']}")

    statuses = []
    for i in range(30):
        statuses.append(
            (await client.post("/v1/favourites", json={"voice_id": f"cap-{i}"})).status_code
        )

    rows = (await client.get("/v1/favourites")).json()["favourites"]
    # Capped, and requests past the cap are refused rather than silently dropped.
    assert len(rows) == 24
    assert 400 in statuses

    # Leave the database as found; the rest of the suite shares it.
    for row in rows:
        await client.delete(f"/v1/favourites/{row['voice_id']}")


async def test_ordering_rejects_unknown_voices(client):
    r = await client.put("/v1/favourites/order", json=["nonexistent-voice"])
    assert r.status_code in (400, 422)


async def test_ordering_is_honoured(client):
    existing = (await client.get("/v1/favourites")).json()["favourites"]
    for row in existing:
        await client.delete(f"/v1/favourites/{row['voice_id']}")

    for vid in ("order-a", "order-b", "order-c"):
        await client.post("/v1/favourites", json={"voice_id": vid})

    assert (
        await client.put("/v1/favourites/order", json=["order-c", "order-a", "order-b"])
    ).status_code == 200
    ids = [f["voice_id"] for f in (await client.get("/v1/favourites")).json()["favourites"]]
    assert ids == ["order-c", "order-a", "order-b"]

    for vid in ids:
        await client.delete(f"/v1/favourites/{vid}")
