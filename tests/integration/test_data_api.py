"""History, usage and export endpoints — the "what did I spend and what did I make" surface."""

from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_history_is_empty_before_anything_is_synthesised(client) -> None:
    r = await client.get("/v1/history")
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_history_records_a_synthesis(client, ctx) -> None:
    from tests.fakes import FakeProvider, install

    install(ctx, FakeProvider(ctx.settings, name="fake"))
    await client.post("/v1/tts", json={"text": "hello there", "voice": "fake-voice"})

    r = await client.get("/v1/history")
    items = r.json()["items"]
    assert items, "synthesis did not appear in history"
    assert items[0]["voice_id"] == "fake-voice"


@pytest.mark.asyncio
async def test_history_search_filters(client, ctx) -> None:
    from tests.fakes import FakeProvider, install

    install(ctx, FakeProvider(ctx.settings, name="fake"))
    await client.post("/v1/tts", json={"text": "alpha bravo", "voice": "fake-voice"})
    await client.post("/v1/tts", json={"text": "charlie delta", "voice": "fake-voice"})

    found = await client.get("/v1/history", params={"q": "alpha"})
    assert len(found.json()["items"]) == 1
    empty = await client.get("/v1/history", params={"q": "zulu"})
    assert empty.json()["items"] == []


@pytest.mark.asyncio
async def test_delete_history_record(client, ctx) -> None:
    from tests.fakes import FakeProvider, install

    install(ctx, FakeProvider(ctx.settings, name="fake"))
    await client.post("/v1/tts", json={"text": "delete me", "voice": "fake-voice"})
    record_id = (await client.get("/v1/history")).json()["items"][0]["id"]

    gone = await client.delete(f"/v1/history/{record_id}")
    assert gone.status_code == 200
    assert (await client.get("/v1/history")).json()["items"] == []


@pytest.mark.asyncio
async def test_delete_unknown_history_record_is_404(client) -> None:
    r = await client.delete("/v1/history/nope")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_usage_reports_a_period_and_totals(client) -> None:
    r = await client.get("/v1/usage")
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"period", "totals", "by_provider", "free_tier"}
    assert body["totals"]["characters"] == 0


@pytest.mark.asyncio
async def test_usage_counts_characters_after_synthesis(client, ctx) -> None:
    from tests.fakes import FakeProvider, install

    install(ctx, FakeProvider(ctx.settings, name="fake"))
    await client.post("/v1/tts", json={"text": "twelve chars", "voice": "fake-voice"})

    body = (await client.get("/v1/usage")).json()
    assert body["totals"]["characters"] >= len("twelve chars")


@pytest.mark.asyncio
async def test_usage_window_is_bounded(client) -> None:
    """A caller must not be able to ask for a 100-year rollup and stall the query."""
    assert (await client.get("/v1/usage", params={"days": 0})).status_code == 422
    assert (await client.get("/v1/usage", params={"days": 100000})).status_code == 422
    assert (await client.get("/v1/usage", params={"days": 365})).status_code == 200
