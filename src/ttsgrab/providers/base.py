"""The ``TTSProvider`` contract every backend implements.

Adding a provider = one subclass + one registration call. Nothing else in the codebase
needs to know it exists.

Guiding rule: **never silently ignore a parameter**. Either honour it, honour it via
post-processing (and say so in ``transcoded``), or report it in ``unsupported``.
"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import ClassVar

from ttsgrab.core.types import (
    Capabilities,
    CostClass,
    ProviderStatus,
    StabilityRisk,
    SynthRequest,
    SynthResult,
    Voice,
)


class ParamSupportError(Exception):
    """Raised by a provider when ``strict`` is on and a param cannot be honoured."""

    def __init__(self, params: list[str], provider: str) -> None:
        self.params = params
        self.provider = provider
        super().__init__(f"{provider} cannot honour: {', '.join(params)}")


class TTSProvider(abc.ABC):
    """Abstract base for all providers."""

    # `name`/`note` are class-level defaults, but WorkerProvider legitimately overrides them
    # per instance (one class, many workers), so they are declared as plain attributes.
    name: str
    kind: str = "cloud"  # cloud | local | worker | builtin
    official: ClassVar[bool] = True
    stability: ClassVar[StabilityRisk] = StabilityRisk.STABLE
    cost_class: ClassVar[CostClass] = CostClass.FREE
    tos_url: ClassVar[str | None] = None
    note: str | None = None

    def __init__(self, settings) -> None:
        self.settings = settings
        self._voices: list[Voice] = []

    # -- lifecycle ---------------------------------------------------------------------
    async def startup(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Called once at app start. Must not raise on missing credentials."""

    async def shutdown(self) -> None:  # noqa: B027 - optional hook, not abstract
        """Called once at app stop."""

    async def refresh_voices(self) -> list[Voice]:
        """Fetch (and cache) the provider's voice list."""
        self._voices = await self.list_voices()
        return self._voices

    @property
    def cached_voices(self) -> list[Voice]:
        return self._voices

    # -- required ----------------------------------------------------------------------
    @property
    @abc.abstractmethod
    def enabled(self) -> bool:
        """False when credentials are missing or the user disabled it."""

    @property
    def disabled_reason(self) -> str | None:
        """Human-readable explanation shown in the UI when ``enabled`` is False."""
        return None

    @property
    @abc.abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abc.abstractmethod
    async def list_voices(self) -> list[Voice]: ...

    @abc.abstractmethod
    async def synthesize(self, req: SynthRequest) -> SynthResult: ...

    # -- optional ----------------------------------------------------------------------
    async def stream(self, req: SynthRequest) -> AsyncIterator[bytes]:
        """Yield audio chunks. Default: synthesise then chunk.

        Providers with real streaming override this; the gateway reports
        ``capabilities.streaming`` accordingly.
        """
        result = await self.synthesize(req)
        yield result.audio

    async def health(self) -> bool:
        return self.enabled

    def status(self) -> ProviderStatus:
        return ProviderStatus(
            name=self.name,
            enabled=self.enabled,
            kind=self.kind,  # type: ignore[arg-type]
            official=self.official,
            stability=self.stability,
            cost_class=self.cost_class,
            reason=self.disabled_reason,
            voices_cached=len(self._voices),
            note=self.note,
            tos_url=self.tos_url,
        )

    # -- helpers for subclasses ---------------------------------------------------------
    def unsupported_for_voice(self, voice: Voice, req: SynthRequest) -> list[str]:
        """Diff requested params against what this voice/provider can do."""
        caps = self.capabilities
        bad: list[str] = []
        if req.format not in caps.formats and req.format not in voice.formats:
            bad.append("format")
        if req.sample_rate and caps.sample_rates and req.sample_rate not in caps.sample_rates:
            bad.append("sample_rate")
        if not caps.rate and abs(req.speed - 1.0) > 1e-6:
            bad.append("speed")
        if not caps.pitch and abs(req.pitch) > 1e-6:
            bad.append("pitch")
        if not caps.volume and abs(req.volume - 1.0) > 1e-6:
            bad.append("volume")
        if req.ssml and not caps.ssml:
            bad.append("ssml")
        if req.reference_audio_id and not caps.cloning:
            bad.append("reference_audio_id")
        return bad
