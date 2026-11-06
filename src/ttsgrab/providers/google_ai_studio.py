"""Google AI Studio TTS adapter (Gemini API, ``generateContent`` with audio modality).

Why this is separate from ``google_cloud``
------------------------------------------
Gemini 3.8 Flash TTS is GA on the **Gemini API / AI Studio**, but explicitly *not* available
through the Cloud Text-to-Speech API. Different endpoint, different auth (API keys created
at aistudio.google.com, not service accounts), different pricing.

Free and paid keys are genuinely different
------------------------------------------
An AI Studio key with billing attached and one without behave differently: the free tier
allows a small *number of requests per day* (Gemini TTS was cut to 10 RPD in November 2026),
while a billed key has a much larger allowance. So the pool keeps two buckets, spends the
free allowance first, and counts requests per Pacific day — the boundary Google uses. A
free key that is out of requests is skipped rather than retried until it 429s, because the
resulting error ("quota exceeded") would otherwise look like an outage to the user.

Keys never leave :mod:`ttsgrab.core.keypool`; the UI sees labels and counters only.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from ttsgrab.core.errors import (
    ProviderError,
    ProviderUnavailable,
    UpstreamAuthError,
    UpstreamRateLimited,
)
from ttsgrab.core.keypool import KeyPool
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


@register
class GoogleAIStudioProvider(TTSProvider):
    """Google AI Studio (Gemini API) speech generation."""

    name = "google_ai_studio"
    kind = "cloud"
    official = True
    stability = StabilityRisk.STABLE
    cost_class = CostClass.PAID
    tos_url = "https://ai.google.dev/gemini-api/terms"
    note = (
        "Gemini 3.8 Flash TTS via the AI Studio API. Free-tier keys allow only a few "
        "requests per day; add a billed key for volume."
    )

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._cfg = settings.google_ai_studio
        self._client: httpx.AsyncClient | None = None
        self.pool = KeyPool(
            free_keys=self._cfg.free_api_keys,
            paid_keys=self._cfg.paid_api_keys,
            free_rpd=self._cfg.free_rpd,
            strategy=self._cfg.strategy,
        )

    # -- key pool management (called from the admin API) --------------------------------
    def add_key(self, secret: str, *, kind: str = "free") -> str:
        """Add a key, and switch the provider on.

        Adding a key *is* an expression of intent — nobody pastes an API key into a provider
        they do not want used. Leaving it disabled would produce the worst kind of bug
        report: "the key is right there in the list and it still does not work".
        """
        if kind not in ("free", "paid"):
            raise ValueError("kind must be 'free' or 'paid'")
        label = self.pool.add(secret, kind=kind)  # type: ignore[arg-type]
        self._cfg.enabled = True
        return label

    def remove_key(self, label: str) -> bool:
        return self.pool.remove(label)

    def pool_status(self) -> dict[str, Any]:
        return {"keys": self.pool.public(), **self.pool.summary()}

    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled and len(self.pool))

    @property
    def disabled_reason(self) -> str | None:
        if not self._cfg.enabled:
            return "google_ai_studio.enabled=false"
        if not len(self.pool):
            return (
                "No API keys: create one at aistudio.google.com, then set "
                "GOOGLE_AI_STUDIO__FREE_API_KEYS (or add it in Providers)"
            )
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
    def _pick_key(self):
        if self.pool.empty:
            raise ProviderUnavailable(self.name, "no API keys configured")
        return self.pool.pick()

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
        attempts = max(1, min(len(self.pool), 3))
        for _ in range(attempts):
            key = self._pick_key()
            key.inflight += 1
            self.pool.record_use(key)
            url = f"{self._cfg.base_url}/models/{self._cfg.model}:generateContent"
            try:
                resp = await self._client.post(
                    url, json=payload, headers={"x-goog-api-key": key.secret}
                )
            except httpx.HTTPError as exc:
                last_error = ProviderError(self.name, f"request failed: {exc}")
                self.pool.penalise(key, cooldown_s=10)
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
                    used_free_tier=(key.kind == "free"),
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    extra={"key": key.label, "key_kind": key.kind, "model": self._cfg.model},
                )

            if resp.status_code in (401, 403):
                self.pool.penalise(key, cooldown_s=300)
                last_error = UpstreamAuthError(self.name, f"key {key.label} rejected")
                continue
            if resp.status_code == 429:
                self.pool.penalise(key, cooldown_s=60)
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
