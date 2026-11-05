"""Azure AI Speech (official).

Included because it is the safest *cloud* default for Traditional Chinese: it has real
``zh-TW`` neural voices and, on the free F0 tier, it **fails closed** — throttling with
HTTP 429 instead of silently billing you, unlike Google.

Auth: subscription key + region, from env or Docker secret.
"""

from __future__ import annotations

import time

import httpx

from ttsgrab.core.errors import (
    ProviderError,
    ProviderUnavailable,
    UpstreamAuthError,
    UpstreamRateLimited,
)
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

AZURE_FREE_CHARS = 500_000

# Azure's per-request rate parameter is a relative percentage string.
_OUTPUT_FORMAT = {
    AudioFormat.MP3: "audio-24khz-48kbitrate-mono-mp3",
    AudioFormat.WAV: "riff-24khz-16bit-mono-pcm",
    AudioFormat.OPUS: "ogg-24khz-16bit-mono-opus",
}


@register
class AzureProvider(TTSProvider):
    name = "azure"
    kind = "cloud"
    official = True
    stability = StabilityRisk.STABLE
    cost_class = CostClass.FREE_TIER
    tos_url = "https://azure.microsoft.com/support/legal/"
    note = "Official. Free F0 tier FAILS CLOSED (HTTP 429) — no surprise invoice."

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._cfg = settings.azure
        self._client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled and self._cfg.api_key and self._cfg.region)

    @property
    def disabled_reason(self) -> str | None:
        if not self._cfg.enabled:
            return "azure.enabled=false"
        if not self._cfg.api_key:
            return "No subscription key: set AZURE_SPEECH_KEY"
        if not self._cfg.region:
            return "azure.region is not set"
        return None

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0))

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OPUS],
            sample_rates=[8000, 16000, 22050, 24000, 44100, 48000],
            rate=True,
            pitch=True,
            volume=True,
            ssml=True,
            streaming=True,
            cloning=False,
            max_chars=5000,
        )

    @property
    def _endpoint(self) -> str:
        return f"https://{self._cfg.region}.tts.speech.microsoft.com"

    async def list_voices(self) -> list[Voice]:
        if not self.enabled:
            return []
        assert self._client is not None
        try:
            resp = await self._client.get(
                f"{self._endpoint}/cognitiveservices/voices/list",
                headers={"Ocp-Apim-Subscription-Key": self._cfg.api_key or ""},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, f"voice listing failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(self.name, f"voice listing HTTP {resp.status_code}")

        voices: list[Voice] = []
        for item in resp.json():
            if item.get("VoiceType") != "Neural":
                continue
            styles: list[str] = []
            for s in item.get("StyleList", []) or []:
                if s and s != "general":
                    styles.append(s)
            voices.append(
                Voice(
                    provider=self.name,
                    voice_id=item["ShortName"],
                    name=item.get("DisplayName", item["ShortName"]),
                    languages=[item.get("Locale", "en-US")],
                    gender={"Male": "male", "Female": "female"}.get(
                        item.get("Gender", ""), "unknown"
                    ),  # type: ignore[arg-type]
                    styles=styles,
                    cost_class=CostClass.FREE_TIER,
                    tier="neural",
                    streaming=True,
                    ssml=True,
                    sample_rates=[8000, 16000, 22050, 24000, 44100, 48000],
                    formats=list(_OUTPUT_FORMAT),
                )
            )
        voices.sort(key=lambda v: (v.languages[0], v.voice_id))
        return voices

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        if not self.enabled:
            raise ProviderUnavailable(self.name, self.disabled_reason or "disabled")
        assert self._client is not None

        bad: list[str] = []
        if req.format not in _OUTPUT_FORMAT:
            bad.append("format")
        if req.reference_audio_id:
            bad.append("reference_audio_id")
        if bad and req.strict:
            raise ParamSupportError(bad, self.name)

        # Azure wants SSML for rate/pitch control even when the text is plain.
        lang = req.language or (
            next((v.languages[0] for v in self._voices if v.voice_id == req.voice_id), None)
            or "en-US"
        )
        if req.ssml:
            ssml = req.text
        else:
            inner = _xml_escape(req.text)
            prosody_attrs = []
            if abs(req.speed - 1.0) > 1e-6:
                prosody_attrs.append(f'rate="{_pct(req.speed)}"')
            if abs(req.pitch) > 1e-6:
                prosody_attrs.append(f'pitch="{req.pitch:+.1f}st"')
            if abs(req.volume - 1.0) > 1e-6:
                prosody_attrs.append(f'volume="{_pct(req.volume)}"')
            style_attr = f' style="{req.style}"' if req.style else ""
            if prosody_attrs:
                inner = f"<prosody {' '.join(prosody_attrs)}>{inner}</prosody>"
            ssml = (
                f'<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" '
                f'xmlns:mstts="https://www.w3.org/2001/mstts" xml:lang="{lang}">'
                f'<voice name="{req.voice_id}"><mstts:express-as{style_attr}>{inner}'
                f"</mstts:express-as></voice></speak>"
            )

        headers = {
            "Ocp-Apim-Subscription-Key": self._cfg.api_key or "",
            "Content-Type": "application/ssml+xml",
            "X-Microsoft-OutputFormat": _OUTPUT_FORMAT.get(
                req.format, _OUTPUT_FORMAT[AudioFormat.MP3]
            ),
            "User-Agent": "ttsgrab",
        }
        started = time.perf_counter()
        try:
            resp = await self._client.post(
                f"{self._endpoint}/cognitiveservices/v1", headers=headers, content=ssml.encode()
            )
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc

        if resp.status_code == 401:
            raise UpstreamAuthError(self.name, "subscription key rejected")
        if resp.status_code == 429:
            raise UpstreamRateLimited(
                self.name, "throttled (F0 free-tier limit or quota exhausted) — no charge incurred"
            )
        if resp.status_code >= 400:
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {resp.text[:300]}")

        return SynthResult(
            audio=resp.content,
            format=req.format,
            sample_rate=24000,
            provider=self.name,
            voice_id=req.voice_id,
            unsupported=sorted(set(bad)),
            billed_characters=len(req.text),
            used_free_tier=self._cfg.tier == "F0",
            latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _pct(multiplier: float) -> str:
    return f"{round((multiplier - 1.0) * 100):+d}%"


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
