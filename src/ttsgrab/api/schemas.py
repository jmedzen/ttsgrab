"""Request/response schemas for the public API."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ttsgrab.core.types import AudioFormat


class TTSRequest(BaseModel):
    """Public synthesis request. Superset of OpenAI's ``/v1/audio/speech`` body."""

    # Hard sanity ceiling against absurd payloads; the real policy limit is
    # `security.max_text_chars`, enforced in the pipeline so it returns 413 (not 422).
    text: str = Field(..., min_length=1, max_length=200_000)
    voice: str | None = Field(
        default=None, description="Voice id, e.g. 'zh-TW-HsiaoChenNeural'. Alias: `voice_id`."
    )
    voice_id: str | None = None
    provider: str | None = Field(default=None, description="Force a provider, e.g. 'edge'.")
    model: str | None = Field(
        default=None, description="OpenAI-compatible alias; ignored unless it maps to a voice."
    )
    format: AudioFormat = AudioFormat.MP3
    response_format: AudioFormat | None = Field(
        default=None, description="OpenAI alias for `format`."
    )
    sample_rate: int | None = Field(default=None, ge=8000, le=48000)
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    pitch: float = Field(default=0.0, ge=-12.0, le=12.0, description="Semitones.")
    volume: float = Field(default=1.0, ge=0.0, le=1.0)
    ssml: bool = False
    style: str | None = None
    prompt: str | None = Field(default=None, description="Style prompt (Gemini TTS).")
    language: str | None = None
    reference_audio_id: str | None = None
    policy: (
        Literal["cheapest", "fastest", "best_quality", "local_only", "free_only", "explicit"] | None
    ) = None
    allow_fallback: bool = True
    strict: bool = Field(
        default=False, description="Reject unsupported params instead of degrading."
    )
    bypass_cache: bool = False
    response: Literal["audio", "json"] = Field(
        default="audio", description="`audio` streams bytes; `json` returns base64 + URL."
    )
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("voice_id")
    @classmethod
    def _v(cls, v: str | None) -> str | None:
        return v

    def resolved_voice(self) -> str:
        voice = self.voice_id or self.voice
        if not voice:
            raise ValueError("either `voice` or `voice_id` is required")
        return voice

    def resolved_format(self) -> AudioFormat:
        return self.response_format or self.format


class TTSResponse(BaseModel):
    id: str
    history_id: str | None = None
    provider: str
    voice_id: str
    format: str
    mime_type: str
    audio_base64: str | None = None
    audio_url: str | None = None
    bytes: int = 0
    duration_ms: int | None = None
    latency_ms: int | None = None
    first_byte_ms: int | None = None
    characters: int = 0
    cost_micro_usd: int = 0
    cost_usd: float = 0.0
    used_free_tier: bool = True
    cached: bool = False
    unsupported: list[str] = Field(default_factory=list)
    transcoded: list[str] = Field(default_factory=list)
    fallback_used: bool = False
    requested_provider: str | None = None
    policy: str | None = None
    attempts: list[dict[str, Any]] = Field(default_factory=list)


class VoiceOut(BaseModel):
    provider: str
    voice_id: str
    name: str
    languages: list[str]
    gender: str
    styles: list[str]
    cost_class: str
    tier: str | None
    streaming: bool
    cloning: bool
    ssml: bool
    price_usd_per_mchar: float
    free_chars_per_month: int = 0


class ProviderOut(BaseModel):
    name: str
    enabled: bool
    kind: str
    official: bool
    stability: str
    cost_class: str
    reason: str | None
    note: str | None
    tos_url: str | None
    voices: int
    health: dict[str, Any] = Field(default_factory=dict)


class UsageOut(BaseModel):
    period: dict[str, str]
    totals: dict[str, Any]
    by_provider: dict[str, Any]
    free_tier: dict[str, Any]
    budgets: dict[str, Any]


class HistoryItem(BaseModel):
    id: str
    created_at: str
    text: str
    text_chars: int
    provider: str
    voice_id: str
    requested_provider: str | None
    format: str
    latency_ms: int | None
    duration_ms: int | None
    audio_bytes: int
    cost_micro_usd: int
    audio_url: str | None
    params: dict[str, Any]
    from_cache: bool


class ApiKeyCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    provider_allowlist: list[str] = Field(default_factory=list)
    rate_limit_per_minute: int | None = Field(default=None, ge=1, le=10000)
    monthly_char_quota: int | None = Field(default=None, ge=1000)
    monthly_cost_quota_micro_usd: int | None = None
    note: str | None = None


class ApiKeyOut(BaseModel):
    id: str
    name: str
    prefix: str
    created_at: str
    last_used_at: str | None
    revoked: bool
    provider_allowlist: list[str]
    rate_limit_per_minute: int | None
    monthly_char_quota: int | None


class ApiKeyCreated(ApiKeyOut):
    token: str = Field(..., description="Shown ONCE. Store it now; only a hash is kept.")


class WorkerIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    base_url: str
    kind: Literal["tts", "openai_compat"] = "tts"
    enabled: bool = True
    model_hint: str | None = None
    price_micro_usd_per_mchar: int = 0
    auth_header: str | None = None


class WorkerOut(BaseModel):
    id: str
    name: str
    base_url: str
    kind: str
    enabled: bool
    healthy: bool
    consecutive_failures: int
    last_checked_at: str | None
    last_error: str | None
    latency_ms_ewma: float | None
    voices: int
    model_hint: str | None


class JobCreate(TTSRequest):
    webhook_url: str | None = None


class JobOut(BaseModel):
    id: str
    status: str
    progress: float
    created_at: str
    updated_at: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    webhook_status: int | None


class CompareRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=5000)
    targets: list[dict[str, str]] = Field(
        ..., description="List of {provider, voice_id} to compare."
    )
    format: AudioFormat = AudioFormat.MP3
    speed: float = 1.0
    pitch: float = 0.0
