"""A scriptable fake provider used by routing/fallback/cache tests.

Lets a test say "provider A fails twice, then succeeds" without touching the network.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

from ttsgrab.core.types import (
    AudioFormat,
    Capabilities,
    CostClass,
    StabilityRisk,
    SynthRequest,
    SynthResult,
    Voice,
)
from ttsgrab.providers.base import ParamSupportError, TTSProvider


# A minimal but real 16-bit PCM WAV (0.1 s of silence at 24 kHz), so downstream
# duration probing and format sniffing behave like they would in production.
def _silence_wav(ms: int = 100, rate: int = 24000) -> bytes:
    import struct

    frames = int(rate * ms / 1000)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVE"
    header += b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
    header += b"data" + struct.pack("<I", len(data))
    return header + data


SILENT_WAV = _silence_wav()


@dataclass
class FakeBehaviour:
    """How the fake provider should behave for the next N calls."""

    failures: int = 0  # number of upcoming calls that raise
    error: str = "boom"
    latency_ms: int = 10
    formats: list[AudioFormat] = field(default_factory=lambda: [AudioFormat.WAV])
    raise_unsupported: bool = False


class FakeProvider(TTSProvider):
    def __init__(
        self,
        settings=None,
        *,
        name: str = "fake",
        voices: list[Voice] | None = None,
        enabled: bool = True,
        behaviour: FakeBehaviour | None = None,
        cost_class: CostClass = CostClass.FREE,
        kind: str = "local",
    ) -> None:
        super().__init__(settings)
        self.name = name
        self.kind = kind
        self.cost_class = cost_class
        self.stability = StabilityRisk.STABLE
        self.official = True
        self._enabled = enabled
        self.behaviour = behaviour or FakeBehaviour()
        self.calls: list[SynthRequest] = []
        self._voices = voices or [
            Voice(
                provider=name,
                voice_id=f"{name}-voice",
                name=f"{name} voice",
                languages=["en-US"],
                formats=[AudioFormat.WAV, AudioFormat.MP3],
                sample_rates=[24000],
                cost_class=cost_class,
            )
        ]

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def disabled_reason(self) -> str | None:
        return None if self._enabled else "test-disabled"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=self.behaviour.formats,
            sample_rates=[24000],
            rate=True,
            pitch=True,
            volume=True,
            ssml=False,
            streaming=True,
            cloning=False,
            max_chars=10000,
        )

    async def list_voices(self) -> list[Voice]:
        return self._voices

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        self.calls.append(req)
        if self.behaviour.failures > 0:
            self.behaviour.failures -= 1
            raise RuntimeError(self.behaviour.error)
        if self.behaviour.raise_unsupported:
            raise ParamSupportError(["speed"], self.name)
        bad = self.unsupported_for_voice(
            next((v for v in self._voices if v.voice_id == req.voice_id), None), req
        )
        if bad and req.strict:
            raise ParamSupportError(bad, self.name)
        await asyncio.sleep(self.behaviour.latency_ms / 1000)
        return SynthResult(
            audio=SILENT_WAV,
            format=AudioFormat.WAV,
            sample_rate=24000,
            provider=self.name,
            voice_id=req.voice_id,
            billed_characters=len(req.text),
            cost_micro_usd=0,
            used_free_tier=True,
            latency_ms=self.behaviour.latency_ms,
        )

    async def stream(self, req: SynthRequest) -> AsyncIterator[bytes]:
        result = await self.synthesize(req)
        yield result.audio


def install(ctx, *providers: FakeProvider) -> None:
    """Replace the registry contents with the given fakes."""
    ctx.registry._instances.clear()
    for p in providers:
        ctx.registry.add_instance(p)
