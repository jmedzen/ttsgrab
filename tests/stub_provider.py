"""A deterministic provider for tests that need real synthesis without a network.

Most of this project's provider tests mock at a lower level. The features checked here —
idempotency, subtitles, loudness, provenance — only misbehave when the *whole* pipeline
runs, so they need a provider that returns genuine audio rather than a fixture.

The audio is a real RIFF/WAVE sine tone: ffmpeg can transcode, normalise, and tag it, so
the post-processing under test is exercised for real. It never appears in production
because the module is only imported from `tests/`.
"""

from __future__ import annotations

import math
import struct

from ttsgrab.core.types import (
    AudioFormat,
    Capabilities,
    CostClass,
    StabilityRisk,
    SynthRequest,
    SynthResult,
    Voice,
)
from ttsgrab.providers.base import TTSProvider

SAMPLE_RATE = 24_000


def make_tone(ms: int, *, freq: float = 220.0, sample_rate: int = SAMPLE_RATE) -> bytes:
    """A mono 16-bit PCM sine wave of the requested length."""
    frames = max(1, int(sample_rate * ms / 1000))
    data = bytearray()
    for i in range(frames):
        value = int(8000 * math.sin(2 * math.pi * freq * i / sample_rate))
        data += struct.pack("<h", value)
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, sample_rate, sample_rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return bytes(header) + bytes(data)


class StubProvider(TTSProvider):
    name = "stub"
    kind = "builtin"
    official = True
    stability = StabilityRisk.STABLE
    # Not FREE: a free provider is preferred by the cheapest policy, and these tests should
    # not accidentally depend on ranking.
    note = "test-only tone generator"

    @property
    def enabled(self) -> bool:
        return True

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.WAV, AudioFormat.MP3, AudioFormat.OPUS, AudioFormat.FLAC],
            sample_rates=[SAMPLE_RATE],
            max_chars=100_000,
        )

    async def list_voices(self) -> list[Voice]:
        return [
            Voice(
                provider=self.name,
                voice_id="stub-tone",
                name="Stub Tone",
                languages=["en-US"],
                gender="neutral",
                cost_class=CostClass.FREE,
            )
        ]

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        # 120 ms per character keeps multi-sentence text long enough that loudness
        # measurement is meaningful, while staying fast in tests.
        ms = max(200, 120 * len(req.text))
        audio = make_tone(ms)
        if req.format is not AudioFormat.WAV:
            from ttsgrab.core.audio import transcode

            audio = await transcode(
                audio,
                src_format=AudioFormat.WAV,
                dst_format=req.format,
                sample_rate=req.sample_rate,
            )
        return SynthResult(
            audio=audio,
            format=req.format,
            sample_rate=req.sample_rate or SAMPLE_RATE,
            duration_ms=ms,
            provider=self.name,
            voice_id=req.voice_id or "stub-tone",
            billed_characters=len(req.text),
            cost_micro_usd=0,
            used_free_tier=True,
            latency_ms=0,
            from_cache=False,
        )


class FailingProvider(TTSProvider):
    """A provider that always errors, so a fallback is guaranteed.

    Needed to test the *positive* case of degradation: a caller who asked for the better
    engine and silently received the worse one.
    """

    name = "stub_failing"
    kind = "builtin"
    official = True
    stability = StabilityRisk.STABLE
    note = "test-only: always raises"

    @property
    def enabled(self) -> bool:
        return True

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(formats=[AudioFormat.MP3], max_chars=100_000)

    async def list_voices(self) -> list[Voice]:
        return [
            Voice(
                provider=self.name,
                voice_id="stub-tone",
                name="Stub Failing",
                languages=["en-US"],
                gender="neutral",
                cost_class=CostClass.FREE,
            )
        ]

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        from ttsgrab.core.errors import ProviderError

        raise ProviderError("the failing provider always fails")
