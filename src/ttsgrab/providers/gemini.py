"""Gemini TTS adapter (Gemini API, ``generateContent`` with audio modality).

Why this is separate from ``google_cloud``
------------------------------------------
Gemini 3.8 Flash TTS is GA on the **Agent Platform / Gemini API**, but explicitly *not*
available through the Cloud Text-to-Speech API. Different endpoint, different auth
(API keys, not service accounts), different pricing, and **no free tier at all**.

The "free vs paid key pool" requirement lives here: Gemini API keys created in AI Studio
carry free-tier rate limits; keys with billing attached do not. The pool tries cheap
(free) keys first, tracks per-key failures, and fails over — without ever leaking a key
into logs or responses.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ttsgrab.core.errors import (
    ProviderError,
    ProviderUnavailable,
    UpstreamAuthError,
    UpstreamRateLimited,
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

# Documented Gemini-TTS prebuilt voices (30 names, shared across Gemini TTS models).
GEMINI_VOICES: tuple[tuple[str, str], ...] = (
    ("Zephyr", "female"),
    ("Puck", "male"),
    ("Charon", "male"),
    ("Kore", "female"),
    ("Fenrir", "male"),
    ("Leda", "female"),
    ("Orus", "male"),
    ("Aoede", "female"),
    ("Callirrhoe", "female"),
    ("Autonoe", "female"),
    ("Enceladus", "male"),
    ("Iapetus", "male"),
    ("Umbriel", "male"),
    ("Algieba", "male"),
    ("Despina", "female"),
    ("Erinome", "female"),
    ("Algenib", "male"),
    ("Rasalgethi", "male"),
    ("Laomedeia", "female"),
    ("Achernar", "female"),
    ("Alnilam", "male"),
    ("Schedar", "male"),
    ("Gacrux", "female"),
    ("Pulcherrima", "female"),
    ("Achird", "male"),
    ("Zubenelgenubi", "male"),
    ("Vindemiatrix", "female"),
    ("Sadachbia", "male"),
    ("Sadaltager", "male"),
    ("Sulafat", "female"),
)

# Languages documented for Gemini 3.8 Flash TTS. Gemini is multilingual and does not
# bind a voice to a language, so every voice advertises the full set.
GEMINI_LANGUAGES = [
    "zh-TW",
    "zh-CN",
    "en-US",
    "en-GB",
    "ja-JP",
    "ko-KR",
    "fr-FR",
    "de-DE",
    "es-ES",
    "it-IT",
    "pt-BR",
    "hi-IN",
    "ar-XA",
    "ru-RU",
    "nl-NL",
    "pl-PL",
    "th-TH",
    "vi-VN",
    "tr-TR",
    "id-ID",
]


@dataclass
class _KeyState:
    """Per-key runtime state for the pool. The secret itself never leaves this object."""

    secret: str
    label: str  # e.g. "free#1", "paid#2" — safe to log
    paid: bool = False
    inflight: int = 0
    failures: int = 0
    last_used: float = 0.0
    cooldown_until: float = 0.0
    requests: int = field(default=0)


class GeminiTTSProvider(TTSProvider):
    name = "gemini"
    kind = "cloud"
    official = True
    stability = StabilityRisk.STABLE
    cost_class = CostClass.PAID
    tos_url = "https://ai.google.dev/gemini-api/terms"
    note = "Gemini 3.8 Flash TTS. No free allowance on Cloud billing; free-API-key pool supported."

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._cfg = settings.gemini
        self._client: httpx.AsyncClient | None = None
        self._keys: list[_KeyState] = []
        self._rr = itertools.count()
        self.pool_summary: list[dict[str, Any]] = []
        self._build_pool()

    def _build_pool(self) -> None:
        self._keys = []
        for i, key in enumerate(self._cfg.api_keys):
            if not key:
                continue
            # Heuristic: keys listed first are treated as free tier. Users can reorder.
            self._keys.append(_KeyState(secret=key, label=f"key#{i + 1}", paid=False))
        self._remember_pool_for_ui()

    def _remember_pool_for_ui(self) -> None:
        self.pool_summary = [
            {"label": k.label, "paid": k.paid, "failures": k.failures, "requests": k.requests}
            for k in self._keys
        ]

    def add_key(self, secret: str, *, paid: bool = False) -> str:
        label = f"{'paid' if paid else 'free'}#{len(self._keys) + 1}"
        self._keys.append(_KeyState(secret=secret, label=label, paid=paid))
        self._remember_pool_for_ui()
        return label

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled and self._keys)

    @property
    def disabled_reason(self) -> str | None:
        if not self._cfg.enabled:
            return "gemini.enabled=false"
        if not self._keys:
            return "No API keys: set GEMINI_API_KEY or add one in the UI"
        return None

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(120.0))

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.WAV, AudioFormat.MP3],  # we transcode to MP3 when asked
            sample_rates=[24000],
            rate=False,  # pace is prompt-steered, not a numeric knob
            pitch=False,
            volume=False,
            ssml=False,
            streaming=False,
            cloning=False,
            max_chars=8000,
        )

    async def list_voices(self) -> list[Voice]:
        if not self.enabled:
            return []
        return [
            Voice(
                provider=self.name,
                voice_id=vid,
                name=f"Gemini {vid}",
                languages=list(GEMINI_LANGUAGES),
                gender=gender,  # type: ignore[arg-type]
                styles=["prompt-steerable"],
                cost_class=CostClass.PAID,
                tier="gemini-tts",
                streaming=False,
                ssml=False,
                sample_rates=[24000],
                formats=[AudioFormat.WAV, AudioFormat.MP3],
                description="Style, accent, pace and emotion are steered by the prompt field.",
                price_micro_usd_per_mchar=0,
            )
            for vid, gender in GEMINI_VOICES
        ]

    # -- key selection ---------------------------------------------------------------------
    def _pick_key(self) -> _KeyState:
        now = time.time()
        usable = [k for k in self._keys if k.cooldown_until <= now]
        if not usable:
            # Everything is cooling down; take the one that recovers soonest.
            usable = sorted(self._keys, key=lambda k: k.cooldown_until)[:1]
        if not usable:
            raise ProviderUnavailable(self.name, "no API keys configured")

        strategy = self._cfg.strategy
        if strategy == "round_robin":
            return usable[next(self._rr) % len(usable)]
        if strategy == "failover":
            return sorted(usable, key=lambda k: k.failures)[0]
        # least_recently_used (default): spread load, prefer free keys
        return sorted(usable, key=lambda k: (k.paid, k.last_used, k.inflight))[0]

    def _penalise(self, key: _KeyState, *, cooldown_s: float) -> None:
        key.failures += 1
        key.cooldown_until = time.time() + cooldown_s

    # -- synthesis ---------------------------------------------------------------------------
    async def synthesize(self, req: SynthRequest) -> SynthResult:
        if not self.enabled:
            raise ProviderUnavailable(self.name, self.disabled_reason or "disabled")
        assert self._client is not None

        bad: list[str] = []
        for param, ok in (
            ("speed", abs(req.speed - 1.0) < 1e-6),
            ("pitch", abs(req.pitch) < 1e-6),
            ("volume", abs(req.volume - 1.0) < 1e-6),
            ("ssml", not req.ssml),
            ("reference_audio_id", not req.reference_audio_id),
        ):
            if not ok:
                bad.append(param)
        if bad and req.strict:
            raise ParamSupportError(sorted(set(bad)), self.name)

        prompt = req.prompt or "Read the following aloud in a natural, clear voice."
        if abs(req.speed - 1.0) > 1e-6:
            pct = "slightly faster" if req.speed > 1 else "slightly slower"
            prompt += f" Speak {pct} than usual."

        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": f"{prompt}\n\n{req.text}"}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": req.voice_id}},
                },
            },
        }

        started = time.perf_counter()
        last_error: Exception | None = None
        attempts = max(1, min(len(self._keys), 3))
        for _ in range(attempts):
            key = self._pick_key()
            key.inflight += 1
            key.last_used = time.time()
            key.requests += 1
            self._remember_pool_for_ui()
            url = f"{self._cfg.base_url}/models/{self._cfg.model}:generateContent"
            try:
                resp = await self._client.post(
                    url, json=payload, headers={"x-goog-api-key": key.secret}
                )
            except httpx.HTTPError as exc:
                last_error = ProviderError(self.name, f"request failed: {exc}")
                self._penalise(key, cooldown_s=10)
                continue
            finally:
                key.inflight -= 1

            if resp.status_code == 200:
                audio, mime = self._extract_audio(resp.json())
                if not audio:
                    last_error = ProviderError(self.name, "no audio in response")
                    continue
                fmt = AudioFormat.WAV if "wav" in (mime or "") else AudioFormat.MP3
                chars = len(req.text)
                return SynthResult(
                    audio=audio,
                    format=fmt,
                    sample_rate=24000,
                    provider=self.name,
                    voice_id=req.voice_id,
                    unsupported=sorted(set(bad)),
                    billed_characters=chars,
                    used_free_tier=not key.paid,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    extra={"key": (key.paid and "paid") or "free", "model": self._cfg.model},
                )

            if resp.status_code in (401, 403):
                self._penalise(key, cooldown_s=300)
                last_error = UpstreamAuthError(self.name, f"key {key.label} rejected")
                continue
            if resp.status_code == 429:
                self._penalise(key, cooldown_s=60)
                last_error = UpstreamRateLimited(self.name, f"key {key.label} rate limited")
                continue
            # 400 usually means the model id or voice name is wrong — do not burn more keys.
            detail = resp.text[:400]
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {detail}")

        raise last_error or ProviderError(self.name, "all keys failed")

    @staticmethod
    def _extract_audio(body: dict[str, Any]) -> tuple[bytes, str | None]:
        import base64

        for cand in body.get("candidates", []) or []:
            for part in cand.get("content", {}).get("parts", []) or []:
                inline = part.get("inlineData") or part.get("inline_data")
                if not inline:
                    continue
                data = inline.get("data")
                if data:
                    return base64.b64decode(data), inline.get("mimeType") or inline.get("mime_type")
        return b"", None
