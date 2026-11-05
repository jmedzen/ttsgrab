"""Google Cloud Text-to-Speech (official REST API).

Auth: service-account JSON (preferred) or an API key. Both come from env/Docker secret,
never from config committed to git.

Cost accounting is the interesting part. Google bills per character per voice tier, with
a monthly free allowance per tier, and — unlike Azure F0 — it *keeps billing* past the
allowance. So the gateway keeps its own ledger and refuses to overspend (see
``core/usage.py``); this adapter only reports what it used.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

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

GOOGLE_TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1"
GOOGLE_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Tier → (label, API name fragment, output format support)
_TIER_ORDER = ("Chirp3-HD", "Studio", "Neural2", "Wavenet", "Standard")

# Google's documented monthly free allowances (characters), verified 2026-11.
FREE_TIER_CHARS: dict[str, int] = {
    "Standard": 4_000_000,
    "Wavenet": 4_000_000,
    "Neural2": 1_000_000,
    "Chirp3-HD": 1_000_000,
    "Studio": 1_000_000,
    "Polyglot": 1_000_000,
    "Gemini-TTS": 0,  # No free allowance at all
}

_AUDIO_ENCODING = {
    AudioFormat.MP3: "MP3",
    AudioFormat.WAV: "LINEAR16",
    AudioFormat.OPUS: "OGG_OPUS",
}


def tier_from_voice_id(voice_id: str) -> str:
    """Map a Google voice id to its pricing tier.

    Ids look like ``<lang>-<REGION>-<Tier>-<Name>``, but the tier segment is not always
    one token: Chirp voices are ``en-US-Chirp3-HD-Achernar``. We therefore scan every
    dash-separated segment after the language code.
    """
    parts = voice_id.split("-")
    segments = [p.lower() for p in parts[2:]] if len(parts) > 2 else []
    joined = "-".join(segments)
    if "chirp3" in joined:
        return "Chirp3-HD"
    for tier in ("Studio", "Neural2", "Wavenet", "Polyglot", "Standard"):
        if tier.lower() in segments:
            return tier
    return "Standard"


@register
class GoogleCloudProvider(TTSProvider):
    name = "google_cloud"
    kind = "cloud"
    official = True
    stability = StabilityRisk.STABLE
    cost_class = CostClass.FREE_TIER
    tos_url = "https://cloud.google.com/terms"
    note = "Official. Overage is BILLED automatically — the gateway enforces your budget cap."

    def __init__(self, settings) -> None:
        super().__init__(settings)
        self._cfg = settings.google_cloud
        self._client: httpx.AsyncClient | None = None
        self._creds = None
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- auth ---------------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._cfg.enabled and (self._cfg.credentials_file or self._cfg.api_key))

    @property
    def disabled_reason(self) -> str | None:
        if not self._cfg.enabled:
            return "google_cloud.enabled=false"
        if not (self._cfg.credentials_file or self._cfg.api_key):
            return "No credentials: set GOOGLE_APPLICATION_CREDENTIALS or GOOGLE_CLOUD_TTS_API_KEY"
        return None

    async def startup(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(45.0))
        if self._cfg.credentials_file:
            path = Path(self._cfg.credentials_file)
            if not path.is_file():  # noqa: ASYNC240 - checked once at startup
                raise FileNotFoundError(f"service account file not found: {path}")

    async def shutdown(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _auth_headers(self) -> dict[str, str]:
        if self._cfg.api_key:
            return {"X-Goog-Api-Key": self._cfg.api_key}
        if not self._cfg.credentials_file:
            raise UpstreamAuthError(self.name, "no credentials configured")

        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return {"Authorization": f"Bearer {self._token}"}

        import asyncio

        def _refresh() -> tuple[str, float]:
            from google.auth.transport.requests import Request
            from google.oauth2 import service_account

            creds = service_account.Credentials.from_service_account_file(
                self._cfg.credentials_file, scopes=GOOGLE_SCOPES
            )
            creds.refresh(Request())
            expiry = creds.expiry.timestamp() if creds.expiry else now + 3000
            return creds.token, expiry

        try:
            token, expiry = await asyncio.to_thread(_refresh)
        except Exception as exc:
            raise UpstreamAuthError(self.name, f"service account auth failed: {exc}") from exc
        self._token, self._token_expiry = token, expiry
        return {"Authorization": f"Bearer {token}"}

    # -- capabilities --------------------------------------------------------------------
    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OPUS],
            sample_rates=[8000, 16000, 22050, 24000, 32000, 44100, 48000],
            rate=True,
            pitch=True,
            volume=False,  # volumeGainDb exists but is emulated; we report it as unsupported
            ssml=True,
            streaming=True,
            cloning=False,
            max_chars=5000,
            supports_multi_speaker=True,
        )

    # -- voices ---------------------------------------------------------------------------
    async def list_voices(self) -> list[Voice]:
        if not self.enabled:
            return []
        assert self._client is not None
        headers = await self._auth_headers()
        url = f"{GOOGLE_TTS_ENDPOINT}/voices"
        if self._cfg.api_key:
            url += f"?key={self._cfg.api_key}"
        try:
            resp = await self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(self.name, f"voice listing failed: {exc}") from exc
        if resp.status_code != 200:
            raise ProviderError(self.name, f"voice listing HTTP {resp.status_code}")
        data = resp.json()

        voices: list[Voice] = []
        for item in data.get("voices", []):
            vid = item["name"]
            langs = item.get("languageCodes", [])
            tier = tier_from_voice_id(vid)
            gender_raw = item.get("ssmlGender", "SSML_VOICE_GENDER_UNSPECIFIED")
            voices.append(
                Voice(
                    provider=self.name,
                    voice_id=vid,
                    name=item.get("name", vid),
                    languages=langs,
                    gender={
                        "MALE": "male",
                        "FEMALE": "female",
                        "NEUTRAL": "neutral",
                    }.get(gender_raw, "unknown"),  # type: ignore[arg-type]
                    cost_class=CostClass.FREE_TIER,
                    tier=tier,
                    streaming=True,
                    ssml=tier not in {"Chirp3-HD"},
                    sample_rates=self._sample_rates_for(tier),
                    formats=self._formats_for(tier),
                    price_micro_usd_per_mchar=0,  # filled from pricing.yaml at catalogue build
                    description=item.get("naturalSampleRateHertz") and f"{tier} voice",
                )
            )
        return voices

    @staticmethod
    def _formats_for(tier: str) -> list[AudioFormat]:
        if tier == "Chirp3-HD":
            # Chirp 3 HD rejects ALAW and SSML, but MP3/WAV/OPUS are fine.
            return [AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OPUS]
        return [AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OPUS]

    @staticmethod
    def _sample_rates_for(tier: str) -> list[int]:
        if tier == "Chirp3-HD":
            return [24000]
        return [8000, 16000, 22050, 24000, 32000, 44100, 48000]

    # -- synthesis -------------------------------------------------------------------------
    async def synthesize(self, req: SynthRequest) -> SynthResult:
        if not self.enabled:
            raise ProviderUnavailable(self.name, self.disabled_reason or "disabled")
        assert self._client is not None

        voice = next((v for v in self._voices if v.voice_id == req.voice_id), None)
        tier = voice.tier if voice else tier_from_voice_id(req.voice_id)

        bad: list[str] = []
        if tier == "Chirp3-HD":
            if abs(req.speed - 1.0) > 1e-6:
                bad.append("speed")
            if abs(req.pitch) > 1e-6:
                bad.append("pitch")
            if req.ssml:
                bad.append("ssml")
        if abs(req.volume - 1.0) > 1e-6:
            bad.append("volume")
        if req.format not in self.capabilities.formats:
            bad.append("format")
        if req.reference_audio_id:
            bad.append("reference_audio_id")
        if bad and req.strict:
            raise ParamSupportError(sorted(set(bad)), self.name)

        payload: dict[str, Any] = {
            "input": {"ssml" if req.ssml else "text": req.text},
            "voice": {
                "languageCode": (
                    voice.languages[0] if voice and voice.languages else req.language or "en-US"
                ),
                "name": req.voice_id,
            },
            "audioConfig": {
                "audioEncoding": _AUDIO_ENCODING.get(req.format, "MP3"),
                "speakingRate": round(req.speed, 3),
                "pitch": round(req.pitch, 2),
            },
        }
        sr = req.sample_rate or (24000 if tier == "Chirp3-HD" else None)
        if sr:
            payload["audioConfig"]["sampleRateHertz"] = sr
        if req.style:
            # Cloud TTS has no per-request style parameter; style is a property of the
            # chosen voice family (e.g. Studio). Record it as unsupported rather than
            # pretending otherwise.
            bad.append("style")

        headers = await self._auth_headers()
        url = f"{GOOGLE_TTS_ENDPOINT}/text:synthesize"
        if self._cfg.api_key:
            url += f"?key={self._cfg.api_key}"

        started = time.perf_counter()
        try:
            resp = await self._client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            raise ProviderError(self.name, f"request failed: {exc}") from exc

        if resp.status_code == 401:
            raise UpstreamAuthError(self.name, "credential rejected")
        if resp.status_code == 429:
            raise UpstreamRateLimited(self.name, "quota exceeded upstream")
        if resp.status_code >= 400:
            detail = resp.text[:400]
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {detail}")

        import base64

        body = resp.json()
        audio = base64.b64decode(body["audioContent"])
        chars = len(req.text)

        return SynthResult(
            audio=audio,
            format=req.format,
            sample_rate=sr or 24000,
            provider=self.name,
            voice_id=req.voice_id,
            unsupported=sorted(set(bad)),
            billed_characters=chars,
            used_free_tier=tier in FREE_TIER_CHARS,
            latency_ms=int((time.perf_counter() - started) * 1000),
            extra={"tier": tier},
        )

    # -- quota introspection ---------------------------------------------------------------
    @staticmethod
    def free_allowance() -> dict[str, int]:
        return dict(FREE_TIER_CHARS)
