"""Local-model worker protocol and client.

A *worker* is any HTTP server on the LAN that implements this minimal contract:

    GET  /health     -> 200 {"status":"ok","model":"kokoro","device":"mps"}
    GET  /voices     -> 200 {"voices":[{"voice_id":..,"name":..,"languages":[..],...}]}
    POST /synthesize -> 200 audio bytes
                        body: {text, voice_id, format, sample_rate, speed, pitch,
                               reference_audio (base64, optional)}

Workers are ordinary providers from the gateway's point of view, so routing, caching,
budgeting and history all work unchanged. The gateway adds health monitoring with latency
tracking and automatic eviction, which is what makes "kill the GPU box and requests keep
working" true rather than aspirational.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ttsgrab.core.errors import (
    ProviderError,
    ProviderUnavailable,
    UpstreamAuthError,
)
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

WORKER_FORMATS = [AudioFormat.WAV, AudioFormat.MP3, AudioFormat.FLAC, AudioFormat.OPUS]


class WorkerClient:
    """Thin HTTP client for a worker's three endpoints."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 120.0,
        auth_header: str | None = None,
        kind: str = "tts",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.kind = kind
        self._auth = auth_header
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout), headers=self._headers())

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": "ttsgrab-worker-client/0.1"}
        if self._auth:
            headers["Authorization"] = self._auth
        return headers

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> dict[str, Any]:
        if self.kind == "openai_compat":
            resp = await self._client.get(f"{self.base_url}/v1/models")
        else:
            resp = await self._client.get(f"{self.base_url}/health")
        if resp.status_code >= 400:
            raise ProviderUnavailable("worker", f"health HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            return {"status": "ok"}

    async def voices(self) -> list[dict[str, Any]]:
        if self.kind == "openai_compat":
            # OpenAI-compatible servers advertise voices inconsistently; try the
            # common ttsgrab extension first, then a static default.
            for path in ("/v1/audio/voices", "/v1/voices"):
                resp = await self._client.get(f"{self.base_url}{path}")
                if resp.status_code == 200:
                    data = resp.json()
                    items = data.get("voices", data if isinstance(data, list) else [])
                    return [v if isinstance(v, dict) else {"voice_id": v} for v in items]
            return [{"voice_id": "alloy", "name": "default"}]

        resp = await self._client.get(f"{self.base_url}/voices")
        if resp.status_code >= 400:
            raise ProviderUnavailable("worker", f"voices HTTP {resp.status_code}")
        data = resp.json()
        return data.get("voices", data if isinstance(data, list) else [])

    async def synthesize(self, payload: dict[str, Any]) -> tuple[bytes, str | None]:
        if self.kind == "openai_compat":
            resp = await self._client.post(
                f"{self.base_url}/v1/audio/speech",
                json={
                    "model": payload.get("model", "tts-1"),
                    "input": payload["text"],
                    "voice": payload.get("voice_id", "alloy"),
                    "response_format": payload.get("format", "wav"),
                    "speed": payload.get("speed", 1.0),
                },
            )
        else:
            resp = await self._client.post(f"{self.base_url}/synthesize", json=payload)
        if resp.status_code in (401, 403):
            raise UpstreamAuthError("worker", "worker rejected credentials")
        if resp.status_code >= 400:
            raise ProviderError("worker", f"synthesize HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content, resp.headers.get("content-type")


class WorkerProvider(TTSProvider):
    """Adapts one worker node into a provider."""

    kind = "worker"
    official = True
    stability = StabilityRisk.EXPERIMENTAL
    cost_class = CostClass.FREE

    def __init__(self, node, settings, client: WorkerClient | None = None) -> None:
        super().__init__(settings)
        self._node = node
        # Provider identity is per-instance here (one class, many workers).
        self.name = node.name
        self.note = node.model_hint or "Self-hosted worker"
        self._client = client or WorkerClient(
            node.base_url,
            auth_header=node.auth_header,
            kind=node.kind,
            timeout=settings.workers.default_timeout_s,
        )
        self._device: str | None = None

    @property
    def worker_node(self):
        return self._node

    @property
    def enabled(self) -> bool:
        return bool(self._node.enabled)

    @property
    def disabled_reason(self) -> str | None:
        return None if self._node.enabled else "worker disabled in the registry"

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=WORKER_FORMATS,
            sample_rates=[],
            rate=True,
            pitch=True,
            volume=True,
            ssml=False,
            streaming=False,
            cloning=True,
            max_chars=5000,
        )

    async def startup(self) -> None:
        await self.refresh_voices()

    async def shutdown(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        payload = await self._client.health()
        self._device = payload.get("device")
        return True

    async def list_voices(self) -> list[Voice]:
        raw = await self._client.voices()
        voices: list[Voice] = []
        for item in raw:
            vid = item.get("voice_id") or item.get("id") or item.get("name")
            if not vid:
                continue
            voices.append(
                Voice(
                    provider=self.name,
                    voice_id=str(vid),
                    name=str(item.get("name", vid)),
                    languages=list(item.get("languages") or item.get("language") or ["zh-TW"]),
                    gender=item.get("gender", "unknown"),
                    styles=list(item.get("styles") or []),
                    cost_class=CostClass.FREE,
                    tier="local",
                    streaming=bool(item.get("streaming", False)),
                    cloning=bool(item.get("cloning", True)),
                    ssml=False,
                    formats=[AudioFormat.WAV, AudioFormat.MP3],
                )
            )
        return voices

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        if not self.enabled:
            raise ProviderUnavailable(self.name, "worker disabled")

        voice = next((v for v in self._voices if v.voice_id == req.voice_id), None)
        bad = self.unsupported_for_voice(voice, req) if voice else []
        if req.ssml:
            bad = [*{*bad, "ssml"}]
        bad = sorted(set(bad))
        if bad and req.strict:
            raise ParamSupportError(bad, self.name)

        payload: dict[str, Any] = {
            "text": req.text,
            "voice_id": req.voice_id,
            "format": req.format.value,
            "sample_rate": req.sample_rate,
            "speed": req.speed,
            "pitch": req.pitch,
            "volume": req.volume,
            "style": req.style,
        }
        if req.reference_audio_id:
            payload["reference_audio"] = req.reference_audio_id

        started = time.perf_counter()
        try:
            audio, content_type = await self._client.synthesize(payload)
        except (UpstreamAuthError, ProviderError, ProviderUnavailable):
            raise
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, f"worker unreachable: {exc}") from exc

        if not audio:
            raise ProviderError(self.name, "worker returned empty audio")

        fmt = _format_from_content_type(content_type) or req.format
        return SynthResult(
            audio=audio,
            format=fmt,
            sample_rate=req.sample_rate,
            provider=self.name,
            voice_id=req.voice_id,
            unsupported=bad,
            billed_characters=len(req.text),
            cost_micro_usd=0,
            used_free_tier=True,
            latency_ms=int((time.perf_counter() - started) * 1000),
            extra={"device": self._device},
        )


def _format_from_content_type(content_type: str | None) -> AudioFormat | None:
    if not content_type:
        return None
    ct = content_type.split(";")[0].strip().lower()
    return {
        "audio/wav": AudioFormat.WAV,
        "audio/x-wav": AudioFormat.WAV,
        "audio/mpeg": AudioFormat.MP3,
        "audio/mp3": AudioFormat.MP3,
        "audio/ogg": AudioFormat.OPUS,
        "audio/flac": AudioFormat.FLAC,
    }.get(ct)
