"""Streaming must actually stream.

This needs a real server, not the in-process ASGI transport: httpx's ASGITransport
collects the whole body before handing it back, which makes every response look
buffered. The failure being guarded against is invisible in a normal API test — the
response is complete and correct, it simply arrives all at once at the end, so a caller
that waited gained nothing over /v1/tts.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import time

import httpx
import pytest
import uvicorn

from tests.fakes import FakeBehaviour, FakeProvider, install
from ttsgrab.core.types import CostClass

LONG_TEXT = "這是一段用來測試串流行為的文字。" * 20


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.asynccontextmanager
async def live_server(app):
    """Run the real app on a real socket, because streaming is a transport property."""
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        for _ in range(200):
            if server.started:
                break
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(task, timeout=10)


def _make_slow(ctx, latency_s: float, name: str = "slow") -> FakeProvider:
    provider = FakeProvider(ctx.settings, name=name, cost_class=CostClass.FREE)
    provider.behaviour = FakeBehaviour(latency_ms=int(latency_s * 1000))
    return provider


async def _timed_stream(base_url: str, payload: dict) -> tuple[float, float, int]:
    """Return (seconds to first byte, seconds to last byte, byte count)."""
    started = time.perf_counter()
    first: float | None = None
    total = 0
    async with (
        httpx.AsyncClient(timeout=120) as client,
        client.stream("POST", f"{base_url}/v1/tts/stream", json=payload) as response,
    ):
        assert response.status_code == 200, await response.aread()
        async for chunk in response.aiter_bytes():
            if first is None:
                first = time.perf_counter() - started
            total += len(chunk)
    finished = time.perf_counter() - started
    assert first is not None, "no audio came back"
    return first, finished, total


@pytest.mark.asyncio
async def test_stream_delivers_first_bytes_before_the_last_sentence(app, ctx) -> None:
    provider = _make_slow(ctx, latency_s=0.3)
    async with live_server(app) as base:
        # uvicorn runs the lifespan again: it rebuilds the registry *and* replaces
        # app.state.ctx. Install into the live context, not the fixture's stale one.
        install(app.state.ctx, provider)
        first, finished, total = await _timed_stream(
            base,
            {
                "text": LONG_TEXT,
                "voice": "slow-voice",
                "format": "wav",
                "bypass_cache": True,
            },
        )
    assert total > 0
    assert first < finished * 0.75, (
        f"first byte at {first * 1000:.0f} ms of a {finished * 1000:.0f} ms response — "
        "the endpoint is buffering instead of streaming"
    )


@pytest.mark.asyncio
async def test_stream_window_limits_work_started_before_first_byte(app, ctx) -> None:
    """Scheduling the whole document up front would commit the entire cost on a paid
    provider before the caller hears anything."""
    provider = _make_slow(ctx, latency_s=0.15)
    async with live_server(app) as base:
        install(app.state.ctx, provider)
        async with (
            httpx.AsyncClient(timeout=120) as client,
            client.stream(
                "POST",
                f"{base}/v1/tts/stream",
                json={
                    "text": LONG_TEXT,
                    "voice": "slow-voice",
                    "format": "wav",
                    "bypass_cache": True,
                },
            ) as response,
        ):
            assert response.status_code == 200
            first_chunk = await response.aiter_bytes().__anext__()
            assert first_chunk
            started = len(provider.calls)
            await response.aclose()
    assert started <= 3, f"{started} chunk requests had started before the first byte arrived"


@pytest.mark.asyncio
async def test_sse_mode_emits_chunk_and_done_events(app, ctx) -> None:
    provider = _make_slow(ctx, latency_s=0.02)
    async with live_server(app) as base:
        install(app.state.ctx, provider)
        async with (
            httpx.AsyncClient(timeout=120) as client,
            client.stream(
                "POST",
                f"{base}/v1/tts/stream?stream_format=sse",
                json={"text": "第一句。第二句。第三句。", "voice": "slow-voice", "format": "wav"},
            ) as response,
        ):
            assert response.status_code == 200
            body = b""
            async for piece in response.aiter_bytes():
                body += piece
    text = body.decode()
    assert "event: chunk" in text
    assert "event: done" in text


@pytest.mark.asyncio
async def test_stream_headers_forbid_proxy_buffering(app, ctx) -> None:
    """Without X-Accel-Buffering a reverse proxy silently undoes the streaming."""
    provider = _make_slow(ctx, latency_s=0.01)
    async with live_server(app) as base:
        install(app.state.ctx, provider)
        async with (
            httpx.AsyncClient(timeout=60) as client,
            client.stream(
                "POST",
                f"{base}/v1/tts/stream",
                json={"text": "hi", "voice": "slow-voice", "format": "wav"},
            ) as response,
        ):
            assert response.headers.get("x-accel-buffering") == "no"
            assert "no-cache" in response.headers.get("cache-control", "")
            async for _ in response.aiter_bytes():
                pass
