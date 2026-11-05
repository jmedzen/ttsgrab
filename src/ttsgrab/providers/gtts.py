"""gTTS (Google Translate) and the raw translate_tts endpoint.

UNOFFICIAL — off by default.
----------------------------
* Google's Terms of Service forbid automated access that circumvents the interface;
  ``translate.google.com/robots.txt`` disallows ``/translate``.
* No SLA, no commercial grant, IP-ban risk at volume.
* The raw endpoint hard-fails above ~200 characters, so chunking is mandatory.

Enabled only via ``free_web.enabled: true``. The UI shows a permanent warning when it is.
"""

from __future__ import annotations

import asyncio
import io
import time
import urllib.parse
from typing import ClassVar

import httpx

from ttsgrab.core.errors import ProviderError, ProviderUnavailable
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

GTTS_SAMPLE_RATE = 24000
# Documented-ish behaviour: the raw endpoint rejects long text. Stay well under.
TRANSLATE_TTS_MAX_CHARS = 180


def _split(text: str, limit: int) -> list[str]:
    """Split on sentence boundaries, then hard-wrap anything still over ``limit``."""
    import re

    if len(text) <= limit:
        return [text]

    sentences = re.split(r"(?<=[。！？!?\.])\s*", text)
    out: list[str] = []
    buf = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(buf) + len(sentence) <= limit:
            buf += sentence
            continue
        if buf:
            out.append(buf)
        buf = sentence
        while len(buf) > limit:
            out.append(buf[:limit])
            buf = buf[limit:]
    if buf:
        out.append(buf)
    return out or [text[:limit]]


@register
class GTTSProvider(TTSProvider):
    name = "gtts"
    kind = "cloud"
    official = False
    stability = StabilityRisk.FRAGILE
    cost_class = CostClass.FREE
    tos_url = "https://policies.google.com/terms"
    note = "Unofficial Google Translate endpoint. Disallowed by Google's ToS; blocks at scale."

    _LANG_MAP: ClassVar[dict[str, str]] = {
        "zh-TW": "zh-TW",
        "zh-CN": "zh-CN",
        "en-US": "en",
        "en-GB": "en",
        "ja-JP": "ja",
        "ko-KR": "ko",
    }

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._cfg = settings.free_web

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled and self._cfg.gtts_enabled)

    @property
    def disabled_reason(self) -> str | None:
        if not self._cfg.enabled:
            return "free_web.enabled=false"
        if not self._cfg.gtts_enabled:
            return "free_web.gtts_enabled=false"
        return None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.MP3],
            sample_rates=[GTTS_SAMPLE_RATE],
            rate=False,  # gTTS offers slow=True only, not arbitrary rate
            pitch=False,
            volume=False,
            ssml=False,
            streaming=False,
            cloning=False,
            max_chars=5000,
        )

    async def list_voices(self) -> list[Voice]:
        voices = [
            Voice(
                provider=self.name,
                voice_id="gtts-zh-TW",
                name="Google Translate 中文（台灣）",
                languages=["zh-TW"],
                gender="neutral",
                cost_class=CostClass.FREE,
                tier="translate",
                formats=[AudioFormat.MP3],
                sample_rates=[GTTS_SAMPLE_RATE],
                description="Unofficial Google Translate voice.",
            ),
            Voice(
                provider=self.name,
                voice_id="gtts-en",
                name="Google Translate English",
                languages=["en-US", "en-GB"],
                gender="neutral",
                cost_class=CostClass.FREE,
                tier="translate",
                formats=[AudioFormat.MP3],
                sample_rates=[GTTS_SAMPLE_RATE],
            ),
        ]
        return voices

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        if not self.enabled:
            raise ProviderUnavailable(self.name, "provider disabled")
        bad = sorted(
            set(
                self.unsupported_for_voice(
                    next((v for v in self._voices if v.voice_id == req.voice_id), self._voices[0]),
                    req,
                )
            )
        )
        if bad and req.strict:
            raise ParamSupportError(bad, self.name)

        lang = "zh-TW" if "zh" in req.voice_id else "en"
        chunks = _split(req.text, 2500)
        started = time.perf_counter()
        parts: list[bytes] = []
        for chunk in chunks:
            data = await asyncio.to_thread(self._synth_blocking, chunk, lang)
            parts.append(data)

        return SynthResult(
            audio=b"".join(parts),
            format=AudioFormat.MP3,
            sample_rate=GTTS_SAMPLE_RATE,
            provider=self.name,
            voice_id=req.voice_id,
            unsupported=bad,
            billed_characters=len(req.text),
            cost_micro_usd=0,
            used_free_tier=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _synth_blocking(self, text: str, lang: str) -> bytes:
        from gtts import gTTS  # imported lazily; optional dependency

        # NB: the 2nd positional arg of gTTS is `tld`, NOT `lang`.
        tts = gTTS(text=text, lang=lang, tld=self._cfg.gtts_tld)
        buf = io.BytesIO()
        tts.write_to_fp(buf)
        return buf.getvalue()


@register
class TranslateTTSProvider(TTSProvider):
    name = "translate_tts"
    kind = "cloud"
    official = False
    stability = StabilityRisk.FRAGILE
    cost_class = CostClass.FREE
    tos_url = "https://policies.google.com/terms"
    note = "Raw translate_tts endpoint. >200 chars returns HTTP 400; chunking is mandatory."

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._cfg = settings.free_web
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled and self._cfg.translate_tts_enabled)

    @property
    def disabled_reason(self) -> str | None:
        if not self._cfg.enabled:
            return "free_web.enabled=false"
        if not self._cfg.translate_tts_enabled:
            return "free_web.translate_tts_enabled=false"
        return None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.MP3],
            sample_rates=[GTTS_SAMPLE_RATE],
            rate=False,
            pitch=False,
            volume=False,
            streaming=True,
            max_chars=200,
        )

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "Mozilla/5.0"},
            follow_redirects=True,
        )

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def list_voices(self) -> list[Voice]:
        return [
            Voice(
                provider=self.name,
                voice_id="translate-tts-auto",
                name="Google Translate TTS (raw)",
                languages=["zh-TW", "en-US", "ja-JP"],
                gender="neutral",
                cost_class=CostClass.FREE,
                tier="translate",
                formats=[AudioFormat.MP3],
                sample_rates=[GTTS_SAMPLE_RATE],
            )
        ]

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        if not self.enabled:
            raise ProviderUnavailable(self.name, "provider disabled")
        assert self._client is not None
        started = time.perf_counter()
        parts = []
        for chunk in _split(req.text, TRANSLATE_TTS_MAX_CHARS):
            params = {
                "ie": "UTF-8",
                "q": chunk,
                "tl": "zh-TW",
                "client": "tw-ob",
            }
            url = "https://translate.google.com/translate_tts?" + urllib.parse.urlencode(params)
            resp = await self._client.get(url)
            if resp.status_code >= 400:
                raise ProviderError(self.name, f"HTTP {resp.status_code}")
            parts.append(resp.content)
        return SynthResult(
            audio=b"".join(parts),
            format=AudioFormat.MP3,
            sample_rate=GTTS_SAMPLE_RATE,
            provider=self.name,
            voice_id=req.voice_id,
            billed_characters=len(req.text),
            used_free_tier=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
