"""Microsoft Edge read-aloud ("edge-tts") adapter.

LEGAL / STABILITY — read before changing defaults
-------------------------------------------------
* **Unofficial.** This drives the websocket endpoint behind Edge's Read Aloud feature,
  not a documented public API. There is no SLA and no commercial-redistribution grant.
  Microsoft's own guidance points commercial users at Azure AI Speech.
* **Fragile by design.** The endpoint is DRM-gated (``Sec-MS-GEC`` tokens) and Microsoft
  rotates the scheme. Known total outages: 2024-10, 2024-11, 2025-08, 2025-12, 2026-01,
  2026-10. Pinned to ``edge-tts>=7.3.0`` which carries the v2 token.
* Kept **enabled by default** by explicit product decision (it is the only zero-setup
  provider), with a warning banner in the UI. Disable with ``edge.enabled: false``.

Everything else in the gateway treats this as an ordinary provider.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator

from ttsgrab.core.errors import ProviderError, ProviderUnavailable, UpstreamAuthError
from ttsgrab.core.registry import register
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

# Edge outputs 24 kHz 48 kbps mono MP3 regardless of request.
EDGE_SAMPLE_RATE = 24000

# Gender is not exposed by the list endpoint; derived from the documented voice names.
_FEMALE_HINTS = (
    "Aria",
    "Jenny",
    "Ava",
    "Emma",
    "Michelle",
    "Ana",
    "HsiaoChen",
    "HsiaoYu",
    "Xiaoxiao",
    "Xiaoyi",
    "Yunxi",
    "Yunyang",
    "Serena",
    "Sonia",
    "Natasha",
    "Libby",
    "Maisie",
    "Neerja",
    "Emily",
    "Amber",
    "Ashley",
    "Cora",
    "Elizabeth",
    "Luna",
    "Melissa",
    "Phoebe",
    "Sara",
    "Nancy",
    "Clara",
    "Candice",
)


def _guess_gender(short_name: str) -> str:
    last = short_name.rsplit("-", 1)[-1].replace("Neural", "")
    return "female" if any(h in last for h in _FEMALE_HINTS) else "male"


def _locale_from_voice(voice_id: str) -> str:
    parts = voice_id.split("-")
    return "-".join(parts[:2]) if len(parts) >= 2 else "en-US"


@register
class EdgeProvider(TTSProvider):
    name = "edge"
    kind = "cloud"
    official = False
    stability = StabilityRisk.FRAGILE
    cost_class = CostClass.FREE
    tos_url = "https://www.microsoft.com/en-us/servicesagreement"
    note = (
        "Unofficial (Microsoft Edge read-aloud). May break without notice; "
        "no commercial-redistribution grant. Personal/dev use only."
    )

    # The endpoint intermittently answers with an empty audio stream ("NoAudioReceived")
    # even for voices that work seconds earlier. This is not a parameter problem; it is
    # load shedding on Microsoft's side. A short retry converts ~most of these into
    # successes at the cost of a few hundred ms.
    MAX_ATTEMPTS = 4
    RETRY_BACKOFF_S = (0.4, 1.0, 1.8)

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._cfg = settings.edge
        self._sem = asyncio.Semaphore(4)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled)

    @property
    def disabled_reason(self) -> str | None:
        return None if self._cfg.enabled else "Disabled by configuration (edge.enabled=false)"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.MP3],
            sample_rates=[EDGE_SAMPLE_RATE],
            rate=True,
            pitch=True,
            volume=True,
            ssml=False,
            streaming=True,
            cloning=False,
            max_chars=10000,
        )

    async def list_voices(self) -> list[Voice]:
        import edge_tts

        try:
            raw = await edge_tts.list_voices()
        except Exception as exc:
            raise ProviderUnavailable(self.name, f"voice listing failed: {exc}") from exc

        voices: list[Voice] = []
        for item in raw:
            short = item.get("ShortName", "")
            if not short.endswith("Neural"):
                continue
            locale = item.get("Locale") or _locale_from_voice(short)
            voices.append(
                Voice(
                    provider=self.name,
                    voice_id=short,
                    name=item.get("FriendlyName", short)
                    .replace("Microsoft ", "")
                    .replace(" Online (Natural) - ", " - "),
                    languages=[locale],
                    gender=_guess_gender(short),  # type: ignore[arg-type]
                    styles=[],
                    cost_class=CostClass.FREE,
                    tier="neural",
                    streaming=True,
                    ssml=False,
                    sample_rates=[EDGE_SAMPLE_RATE],
                    formats=[AudioFormat.MP3],
                    price_micro_usd_per_mchar=0,
                )
            )
        voices.sort(key=lambda v: (v.languages[0], v.voice_id))
        return voices

    async def health(self) -> bool:
        if not self.enabled:
            return False
        try:
            async with asyncio.timeout(10):
                return len(await self.list_voices()) > 0
        except Exception:
            return False

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        if not self.enabled:
            raise ProviderUnavailable(self.name, "provider disabled")

        voice = next((v for v in self._voices if v.voice_id == req.voice_id), None)
        if voice is None and not self._voices:
            # Catalogue not warm yet — accept the raw id rather than failing the request.
            voice = Voice(
                provider=self.name,
                voice_id=req.voice_id,
                name=req.voice_id,
                languages=[_locale_from_voice(req.voice_id)],
            )
        if voice is None:
            raise ProviderError(self.name, f"unknown voice {req.voice_id!r}")

        bad = self.unsupported_for_voice(voice, req)
        if req.format is not AudioFormat.MP3:
            bad = [*{*bad, "format"}]
        if req.sample_rate and req.sample_rate != EDGE_SAMPLE_RATE:
            bad = [*{*bad, "sample_rate"}]
        if req.ssml:
            bad = [*{*bad, "ssml"}]
        bad = sorted(set(bad))
        if bad and req.strict:
            raise ParamSupportError(bad, self.name)

        rate_pct = round((req.speed - 1.0) * 100)
        pitch_hz = round(req.pitch * 12)  # 1 semitone ~ 12 Hz at Edge's scale
        volume_pct = round((req.volume - 1.0) * 100)

        import edge_tts

        kwargs: dict[str, object] = {}
        if rate_pct:
            kwargs["rate"] = f"{rate_pct:+d}%"
        if pitch_hz:
            kwargs["pitch"] = f"{pitch_hz:+d}Hz"
        if volume_pct:
            kwargs["volume"] = f"{volume_pct:+d}%"
        if self._cfg.proxy:
            kwargs["proxy"] = self._cfg.proxy

        started = time.perf_counter()
        first_byte_ms: int | None = None
        buf = bytearray()
        last_error: str = "unknown"

        for attempt in range(self.MAX_ATTEMPTS):
            buf = bytearray()
            first_byte_ms = None
            try:
                async with self._sem:
                    # `kwargs` is assembled from optional prosody settings, so its type is
                    # heterogeneous; the SDK's signature is narrower than reality.
                    communicate = edge_tts.Communicate(
                        req.text,
                        req.voice_id,
                        **kwargs,  # type: ignore[arg-type]
                    )
                    async for chunk in communicate.stream():
                        if chunk.get("type") != "audio":
                            continue
                        data = chunk.get("data") or b""
                        if not data:
                            continue
                        if first_byte_ms is None:
                            first_byte_ms = int((time.perf_counter() - started) * 1000)
                        buf.extend(data)
            except Exception as exc:
                msg = str(exc)
                if "403" in msg or "401" in msg or "Unauthorized" in msg:
                    raise UpstreamAuthError(
                        self.name,
                        "handshake rejected (DRM token rotated?) — try upgrading edge-tts",
                    ) from exc
                last_error = msg
                buf = bytearray()
            else:
                if buf:
                    break
                last_error = "NoAudioReceived (empty stream)"

            if attempt < self.MAX_ATTEMPTS - 1:
                jitter = random.uniform(0, 0.2)
                await asyncio.sleep(self.RETRY_BACKOFF_S[attempt] + jitter)

        if not buf:
            raise ProviderError(
                self.name,
                f"synthesis failed after {self.MAX_ATTEMPTS} attempts: {last_error}",
            )

        return SynthResult(
            audio=bytes(buf),
            format=AudioFormat.MP3,
            sample_rate=EDGE_SAMPLE_RATE,
            provider=self.name,
            voice_id=req.voice_id,
            unsupported=bad,
            billed_characters=len(req.text),
            cost_micro_usd=0,
            used_free_tier=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
            first_byte_ms=first_byte_ms,
        )

    async def stream(self, req: SynthRequest) -> AsyncIterator[bytes]:
        if not self.enabled:
            raise ProviderUnavailable(self.name, "provider disabled")
        import edge_tts

        kwargs: dict[str, object] = {}
        rate_pct = round((req.speed - 1.0) * 100)
        pitch_hz = round(req.pitch * 12)
        volume_pct = round((req.volume - 1.0) * 100)
        if rate_pct:
            kwargs["rate"] = f"{rate_pct:+d}%"
        if pitch_hz:
            kwargs["pitch"] = f"{pitch_hz:+d}Hz"
        if volume_pct:
            kwargs["volume"] = f"{volume_pct:+d}%"
        if self._cfg.proxy:
            kwargs["proxy"] = self._cfg.proxy

        try:
            async with self._sem:
                communicate = edge_tts.Communicate(
                    req.text,
                    req.voice_id,
                    **kwargs,  # type: ignore[arg-type]
                )
                async for chunk in communicate.stream():
                    if chunk.get("type") == "audio" and chunk.get("data"):
                        yield chunk["data"]
        except Exception as exc:
            raise ProviderError(self.name, f"stream failed: {exc}") from exc
