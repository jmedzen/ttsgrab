"""Normalised domain types shared by every provider and the API layer.

Everything a provider needs to know about a request lives in :class:`SynthRequest`,
and everything the gateway needs back lives in :class:`SynthResult`. Providers never
touch HTTP responses, database rows or config objects directly.
"""

from __future__ import annotations

import enum
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class AudioFormat(enum.StrEnum):
    MP3 = "mp3"
    WAV = "wav"
    OPUS = "opus"
    FLAC = "flac"

    @property
    def mime(self) -> str:
        return {
            "mp3": "audio/mpeg",
            "wav": "audio/wav",
            "opus": "audio/ogg",
            "flac": "audio/flac",
        }[self.value]


class CostClass(enum.StrEnum):
    """How a provider's audio is paid for. Drives the ``free_only`` policy."""

    FREE = "free"  # No money changes hands, ever (self-hosted, unofficial endpoints).
    FREE_TIER = "free_tier"  # Metered, free up to a monthly allowance, billed after.
    PAID = "paid"  # Always costs money.


class StabilityRisk(enum.StrEnum):
    STABLE = "stable"  # Officially supported API with an SLA.
    FRAGILE = "fragile"  # Unofficial endpoint that has broken before.
    EXPERIMENTAL = "experimental"  # Ours, or upstream says "preview".


class Voice(BaseModel):
    """One selectable voice in the unified catalogue."""

    model_config = ConfigDict(frozen=True)

    provider: str
    voice_id: str
    name: str
    languages: list[str] = Field(default_factory=list)  # BCP-47-ish, e.g. ["zh-TW", "en-US"]
    gender: Literal["male", "female", "neutral", "unknown"] = "unknown"
    styles: list[str] = Field(default_factory=list)
    cost_class: CostClass = CostClass.FREE
    tier: str | None = None  # Provider-specific tier: standard / wavenet / chirp3-hd / ...
    streaming: bool = False
    cloning: bool = False
    ssml: bool = False
    sample_rates: list[int] = Field(default_factory=list)
    formats: list[AudioFormat] = Field(default_factory=list)
    description: str | None = None
    preview_url: str | None = None
    # Per-1M-character price in micro-USD (1e-6 USD). 0 == free.
    price_micro_usd_per_mchar: int = 0

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.voice_id}"


class Capabilities(BaseModel):
    """What a provider can actually do. Unsupported params are reported, never dropped."""

    formats: list[AudioFormat] = Field(default_factory=lambda: [AudioFormat.MP3])
    sample_rates: list[int] = Field(default_factory=list)
    rate: bool = True  # speaking rate
    pitch: bool = True
    volume: bool = True
    ssml: bool = False
    streaming: bool = False
    cloning: bool = False
    max_chars: int = 5000
    supports_multi_speaker: bool = False


class SynthRequest(BaseModel):
    """A fully normalised synthesis request, independent of any provider's quirks."""

    text: str
    voice_id: str
    provider: str | None = None
    format: AudioFormat = AudioFormat.MP3
    sample_rate: int | None = None
    # Normalised to a multiplier: 1.0 == normal. Providers that speak SSML map this to
    # <prosody rate>; providers with a percentage API map it to their own scale.
    speed: float = 1.0
    # Semitones, same convention as SSML pitch: +2 == two semitones up.
    pitch: float = 0.0
    volume: float = 1.0  # 0.0 - 1.0 linear gain
    ssml: bool = False
    style: str | None = None
    # Free-form provider prompt (Gemini TTS style steering, voice descriptions, ...).
    prompt: str | None = None
    language: str | None = None
    reference_audio_id: str | None = None
    strict: bool = False  # raise on unsupported params instead of degrading
    # EBU R128 target, already resolved to LUFS by the API layer.
    loudness: float | None = None
    # Force sentence-level splitting even for short text, so subtitle timings exist.
    want_segments: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)

    def cache_key(self) -> str:
        """Hash of everything that can change the produced audio.

        A key that omits a field serving two requests with different audio is worse than no
        cache: the caller gets the *other* request's bytes and no way to tell. `loudness` was
        omitted, so `loudness=-16` and `loudness=-23` shared an entry — the second caller
        silently received audio normalised to the first target.
        """
        payload = {
            "t": self.text,
            "v": self.voice_id,
            "p": self.provider,
            "f": self.format.value,
            "sr": self.sample_rate,
            "sp": round(self.speed, 4),
            "pi": round(self.pitch, 4),
            "vo": round(self.volume, 4),
            "ssml": self.ssml,
            "st": self.style,
            "pr": self.prompt,
            "lang": self.language,
            "ref": self.reference_audio_id,
            # Loudness is applied after synthesis, so it does not affect the provider call
            # — which is why it was easy to leave out — but it absolutely affects the bytes
            # we return and cache.
            "ld": None if self.loudness is None else round(self.loudness, 2),
            # A subtitle request splits by sentence and concatenates, which is different
            # audio — not merely extra metadata on the same audio. Without this the plain
            # request's single-chunk result was returned to the subtitle request, which then
            # had no timings to render.
            "seg": self.want_segments,
            "x": self.extra,
        }
        blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SpeechSegment(BaseModel):
    """One synthesised span, positioned in the final audio."""

    text: str
    start_ms: int
    end_ms: int


class SynthResult(BaseModel):
    """What comes back from a provider."""

    audio: bytes
    format: AudioFormat
    sample_rate: int | None = None
    duration_ms: int | None = None
    provider: str
    voice_id: str
    # Params the provider could not honour. Never silently ignored.
    unsupported: list[str] = Field(default_factory=list)
    # Params that were honoured by post-processing (e.g. ffmpeg transcode) rather than
    # by the provider natively.
    transcoded: list[str] = Field(default_factory=list)
    billed_characters: int = 0
    cost_micro_usd: int = 0
    used_free_tier: bool = False
    latency_ms: int = 0
    first_byte_ms: int | None = None
    from_cache: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)
    # Per-segment timing, filled in when the text had to be split. Subtitle generation
    # needs it, and it is measured anyway during synthesis — discarding it would mean
    # re-measuring every chunk just to emit an SRT.
    segments: list[SpeechSegment] = Field(default_factory=list)


class ProviderStatus(BaseModel):
    name: str
    enabled: bool
    kind: Literal["cloud", "local", "worker", "builtin"]
    official: bool
    stability: StabilityRisk
    cost_class: CostClass
    reason: str | None = None  # why it is disabled
    voices_cached: int = 0
    latency_ms_p50: int | None = None
    note: str | None = None
    tos_url: str | None = None
