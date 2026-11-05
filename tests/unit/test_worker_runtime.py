"""Tests for the shared worker runtime.

The worker protocol is the contract between this project and three model images that are
expensive to boot, so it is worth testing without a GPU, a model, or a container.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "workers"))

from _base.runtime import (
    SynthesizeBody,
    TTSEngine,
    VoiceSpec,
    build_app,
    detect_device,
    floats_to_wav,
    pcm_bytes_to_wav,
    wav_duration_ms,
)


class StubEngine(TTSEngine):
    """A fake model: records calls, returns a valid WAV without loading anything."""

    model_name = "stub"
    sample_rate = 16000

    def __init__(self) -> None:
        super().__init__()
        self.load_calls = 0
        self.synth_calls: list[SynthesizeBody] = []
        self.fail_with: Exception | None = None

    async def load(self) -> None:
        self.load_calls += 1

    async def voices(self) -> list[VoiceSpec]:
        return [
            VoiceSpec(voice_id="stub-a", name="Stub A", languages=["en-US"], gender="female"),
            VoiceSpec(voice_id="stub-b", name="Stub B", languages=["zh-TW"], gender="male"),
        ]

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        *,
        speed: float = 1.0,
        pitch: float = 0.0,
        volume: float = 1.0,
        style: str | None = None,
        reference_wav: bytes | None = None,
        reference_transcript: str | None = None,
    ) -> tuple[bytes, int]:
        self.synth_calls.append(
            SynthesizeBody(
                text=text,
                voice_id=voice_id,
                speed=speed,
                pitch=pitch,
                volume=volume,
                style=style,
                reference_transcript=reference_transcript,
            )
        )
        if self.fail_with is not None:
            raise self.fail_with
        # 250 ms of silence, built without numpy so the test needs no numeric stack.
        frames = int(self.sample_rate * 0.25)
        return pcm_bytes_to_wav(b"\x00\x00" * frames, sample_rate=self.sample_rate), (
            self.sample_rate
        )


def make_client(engine: TTSEngine) -> httpx.AsyncClient:
    app = build_app(engine)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker")


def test_detect_device_honours_explicit_preference(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TTSGRAB_DEVICE", "cpu")
    assert detect_device() == "cpu"
    assert detect_device("mps") == "mps"  # an explicit argument beats the environment


def test_detect_device_falls_back_to_cpu_without_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TTSGRAB_DEVICE", raising=False)
    monkeypatch.delenv("TTSGAB_DEVICE", raising=False)
    monkeypatch.setitem(sys.modules, "torch", None)  # importing it now raises
    assert detect_device() == "cpu"


def test_pcm_bytes_to_wav_produces_a_readable_header() -> None:
    pcm = b"\x00\x00" * 800
    wav = pcm_bytes_to_wav(pcm, sample_rate=24000)
    assert wav[:4] == b"RIFF" and wav[8:12] == b"WAVE"
    assert wav_duration_ms(wav) == pytest.approx(800 / 24000 * 1000, abs=2)


def test_floats_to_wav_clamps_out_of_range_samples() -> None:
    pytest.importorskip("numpy", reason="floats_to_wav is a numpy helper")
    # A model that emits |x|>1 must not wrap around into noise.
    wav = floats_to_wav([2.0, -2.0, 0.0, 0.5], 16000)
    payload = struct.unpack("<4h", wav[44:52])
    assert payload[0] == 32767
    assert payload[1] == -32768
    assert payload[2] == 0


def test_wav_duration_ms_returns_none_for_garbage() -> None:
    assert wav_duration_ms(b"not a wav at all") is None


@pytest.mark.asyncio
async def test_health_reports_unloaded_without_crashing() -> None:
    async with make_client(StubEngine()) as client:
        r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["loaded"] is False
    assert body["model"] == "stub"


@pytest.mark.asyncio
async def test_voices_lists_the_engine_catalogue() -> None:
    async with make_client(StubEngine()) as client:
        r = await client.get("/voices")
    assert r.status_code == 200
    voices = r.json()["voices"]
    assert [v["voice_id"] for v in voices] == ["stub-a", "stub-b"]


@pytest.mark.asyncio
async def test_synthesize_returns_audio_and_lazy_loads_once() -> None:
    engine = StubEngine()
    async with make_client(engine) as client:
        first = await client.post(
            "/synthesize", json={"text": "hello", "voice_id": "stub-a", "format": "wav"}
        )
        second = await client.post(
            "/synthesize", json={"text": "again", "voice_id": "stub-a", "format": "wav"}
        )
    assert first.status_code == 200
    assert first.headers["content-type"] == "audio/wav"
    assert first.content[:4] == b"RIFF"
    assert second.status_code == 200
    assert engine.load_calls == 1, "the model must load once, not once per request"


@pytest.mark.asyncio
async def test_unknown_voice_is_a_404_not_a_crash() -> None:
    async with make_client(StubEngine()) as client:
        r = await client.post(
            "/synthesize", json={"text": "hi", "voice_id": "nope", "format": "wav"}
        )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_empty_text_is_rejected_before_loading_a_model() -> None:
    engine = StubEngine()
    async with make_client(engine) as client:
        r = await client.post(
            "/synthesize", json={"text": "", "voice_id": "stub-a", "format": "wav"}
        )
    assert r.status_code == 422
    assert engine.load_calls == 0


@pytest.mark.asyncio
async def test_engine_failure_surfaces_as_500_with_a_json_body() -> None:
    engine = StubEngine()
    engine.fail_with = RuntimeError("CUDA out of memory")
    async with make_client(engine) as client:
        r = await client.post(
            "/synthesize", json={"text": "boom", "voice_id": "stub-a", "format": "wav"}
        )
    assert r.status_code == 500
    body = r.json()
    # The message must reach the operator: "worker is broken" is useless for diagnosis.
    assert "error" in body
    assert "memory" in str(body).lower() or "runtime" in str(body).lower()


@pytest.mark.asyncio
async def test_cloning_voice_requires_reference_audio() -> None:
    engine = StubEngine()

    async def voices_with_clone() -> list[VoiceSpec]:
        return [
            VoiceSpec(
                voice_id="clone-me",
                name="Clone",
                languages=["zh-TW"],
                cloning=True,
            )
        ]

    engine.voices = voices_with_clone  # type: ignore[method-assign]
    async with make_client(engine) as client:
        r = await client.post(
            "/synthesize", json={"text": "hi", "voice_id": "clone-me", "format": "wav"}
        )
    assert r.status_code == 422
    assert "reference" in str(r.json()).lower()


@pytest.mark.asyncio
async def test_length_cap_is_enforced_at_the_worker_too() -> None:
    """The gateway caps text, but a worker is reachable directly on the LAN."""
    engine = StubEngine()
    async with make_client(engine) as client:
        r = await client.post(
            "/synthesize", json={"text": "x" * 20_001, "voice_id": "stub-a", "format": "wav"}
        )
    assert r.status_code == 422
    assert engine.load_calls == 0


@pytest.mark.asyncio
async def test_concurrent_first_requests_load_the_model_once() -> None:
    import asyncio

    engine = StubEngine()
    async with make_client(engine) as client:
        await asyncio.gather(
            *[
                client.post(
                    "/synthesize",
                    json={"text": f"t{i}", "voice_id": "stub-a", "format": "wav"},
                )
                for i in range(4)
            ]
        )
    assert engine.load_calls == 1
