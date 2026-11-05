"""The worker HTTP protocol, exercised through a real ASGI app.

The gateway's acceptance checklist includes "a worker registers, serves audio, dies, and the
request still succeeds". That was verified once by hand with a synthetic worker; these tests
keep the protocol honest without needing a GPU or a model download.

A fake engine is used deliberately: the point is the contract (health / voices /
synthesize, single-flight load, concurrency cap, clear errors), not kokoro's weights.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from workers._base.runtime import EngineInfo, TTSEngine, VoiceSpec, build_app


class FakeEngine(TTSEngine):
    model_name = "fake-asr"
    license_note = "test only"

    def __init__(self, *, load_delay: float = 0.0, fail: bool = False) -> None:
        super().__init__()
        self.load_calls = 0
        self.load_delay = load_delay
        self.fail = fail
        self.active = 0
        self.peak_concurrency = 0

    async def load(self) -> None:
        self.load_calls += 1
        if self.load_delay:
            await asyncio.sleep(self.load_delay)

    async def voices(self) -> list[VoiceSpec]:
        return [
            VoiceSpec(voice_id="plain", name="Plain", languages=["en-US"]),
            VoiceSpec(voice_id="clone", name="Clone", languages=["en-US"], cloning=True),
        ]

    async def synthesize(self, text, voice_id, **kwargs) -> tuple[bytes, int]:
        if self.fail:
            raise ValueError("engine said no")
        self.active += 1
        self.peak_concurrency = max(self.peak_concurrency, self.active)
        try:
            await asyncio.sleep(0.02)
            # Minimal but valid WAV so duration parsing has something real to read.
            import struct

            payload = struct.pack("<h", 0) * 800
            header = (
                b"RIFF"
                + struct.pack("<I", 36 + len(payload))
                + b"WAVEfmt "
                + struct.pack("<IHHIIHH", 16, 1, 1, 24000, 48000, 2, 16)
                + b"data"
                + struct.pack("<I", len(payload))
            )
            return header + payload, 24000
        finally:
            self.active -= 1

    def info(self) -> EngineInfo:
        return EngineInfo(model=self.model_name)


@pytest.fixture
def make_client():
    def _make(engine: TTSEngine, **kwargs):
        app = build_app(engine, **kwargs)
        transport = httpx.ASGITransport(app=app)
        return httpx.AsyncClient(transport=transport, base_url="http://worker")

    return _make


@pytest.mark.asyncio
async def test_health_reports_without_loading_the_model(make_client) -> None:
    """Health must answer fast; a health check that loads a model is not a health check."""
    engine = FakeEngine(load_delay=5.0)
    async with make_client(engine) as client:
        r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["loaded"] is False
    assert engine.load_calls == 0


@pytest.mark.asyncio
async def test_voices_loads_the_model_once(make_client) -> None:
    engine = FakeEngine()
    async with make_client(engine) as client:
        await client.get("/voices")
        await client.get("/voices")
    assert engine.load_calls == 1, "the model was loaded more than once"


@pytest.mark.asyncio
async def test_concurrent_first_requests_load_the_model_once(make_client) -> None:
    """Single-flight: twenty cold requests must not start twenty model loads."""
    engine = FakeEngine(load_delay=0.05)
    async with make_client(engine) as client:
        await asyncio.gather(*[client.get("/health") for _ in range(20)])
        results = await asyncio.gather(*[client.get("/voices") for _ in range(20)])
    assert all(r.status_code == 200 for r in results)
    assert engine.load_calls == 1


@pytest.mark.asyncio
async def test_synthesize_returns_audio_with_metadata(make_client) -> None:
    engine = FakeEngine()
    async with make_client(engine) as client:
        r = await client.post("/synthesize", json={"text": "hello", "voice_id": "plain"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    assert r.content.startswith(b"RIFF")
    assert r.headers["X-Worker-Model"] == "fake-asr"
    assert r.headers["X-Worker-Sample-Rate"] == "24000"
    assert int(r.headers["X-Worker-Duration-Ms"]) >= 0


@pytest.mark.asyncio
async def test_unknown_voice_is_404(make_client) -> None:
    async with make_client(FakeEngine()) as client:
        r = await client.post("/synthesize", json={"text": "hi", "voice_id": "nope"})
    assert r.status_code == 404
    assert "unknown voice_id" in r.text


@pytest.mark.asyncio
async def test_a_cloning_voice_without_a_reference_is_422(make_client) -> None:
    async with make_client(FakeEngine()) as client:
        r = await client.post("/synthesize", json={"text": "hi", "voice_id": "clone"})
    assert r.status_code == 422
    assert "reference_audio" in r.text


@pytest.mark.asyncio
async def test_invalid_base64_reference_is_400(make_client) -> None:
    async with make_client(FakeEngine()) as client:
        r = await client.post(
            "/synthesize",
            json={"text": "hi", "voice_id": "clone", "reference_audio": "not base64!!"},
        )
    assert r.status_code == 400
    assert "base64" in r.text


@pytest.mark.asyncio
async def test_a_valid_reference_reaches_the_engine(make_client) -> None:
    engine = FakeEngine()
    seen: dict[str, object] = {}
    original = engine.synthesize

    async def spy(text, voice_id, **kwargs):
        seen.update(kwargs)
        return await original(text, voice_id, **kwargs)

    engine.synthesize = spy  # type: ignore[method-assign]
    payload = base64.b64encode(b"RIFFfake").decode()
    async with make_client(engine) as client:
        r = await client.post(
            "/synthesize",
            json={"text": "hi", "voice_id": "clone", "reference_audio": payload},
        )
    assert r.status_code == 200, r.text
    assert seen["reference_wav"] == b"RIFFfake"


@pytest.mark.asyncio
async def test_engine_value_error_is_400_not_500(make_client) -> None:
    """A bad request is the caller's fault and must not look like a worker crash."""
    async with make_client(FakeEngine(fail=True)) as client:
        r = await client.post("/synthesize", json={"text": "hi", "voice_id": "plain"})
    assert r.status_code == 400
    assert "engine said no" in r.text


@pytest.mark.asyncio
async def test_concurrency_cap_is_respected(make_client) -> None:
    engine = FakeEngine()
    async with make_client(engine, max_concurrency=2) as client:
        await asyncio.gather(
            *[client.post("/synthesize", json={"text": "x", "voice_id": "plain"}) for _ in range(6)]
        )
    assert engine.peak_concurrency <= 2, f"cap ignored: saw {engine.peak_concurrency}"


@pytest.mark.asyncio
async def test_stats_reflect_traffic(make_client) -> None:
    engine = FakeEngine()
    async with make_client(engine) as client:
        await client.post("/synthesize", json={"text": "x", "voice_id": "plain"})
        health = (await client.get("/health")).json()
    assert health["stats"]["requests"] == 1
    assert health["stats"]["total_ms"] >= 0
