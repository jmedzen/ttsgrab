# Adding a provider

The gateway has three extension points. Which one to use depends on where the model runs,
not on how much you like Python.

| You are adding… | Use | Effort |
|---|---|---|
| A cloud HTTP API | a **provider** in `src/ttsgrab/providers/` | one file, ~120 lines |
| A model that needs torch/CUDA | a **worker** in `workers/` | one file, ~100 lines |
| An existing OpenAI-shaped server | **nothing** — register it as a worker with `kind: openai_compat` | one HTTP call |

---

## Provider: a cloud API in one file

Say you want Deepgram Aura. Create `src/ttsgrab/providers/deepgram.py`:

```python
"""Deepgram Aura TTS adapter.

Official API, $0.015 / 1k chars at the time of writing. Voices are English-only.
Docs: https://developers.deepgram.com/docs/text-to-speech
"""

from __future__ import annotations

import httpx

from ttsgrab.core.errors import ProviderError, UpstreamAuthError, UpstreamRateLimited
from ttsgrab.core.registry import register
from ttsgrab.core.types import (
    AudioFormat, Capabilities, CostClass, StabilityRisk, SynthRequest, SynthResult, Voice,
)
from ttsgrab.providers.base import TTSProvider


@register
class DeepgramProvider(TTSProvider):
    name = "deepgram"
    kind = "cloud"
    official = True
    stability = StabilityRisk.STABLE
    cost_class = CostClass.PAID
    tos_url = "https://deepgram.com/terms"

    @property
    def enabled(self) -> bool:
        return bool(self.settings.deepgram.enabled and self.settings.deepgram.api_key)

    @property
    def disabled_reason(self) -> str | None:
        if not self.settings.deepgram.enabled:
            return "deepgram.enabled=false"
        if not self.settings.deepgram.api_key:
            return "deepgram.api_key is not set"
        return None

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            formats=[AudioFormat.MP3, AudioFormat.WAV, AudioFormat.OPUS, AudioFormat.FLAC],
            sample_rates=[8000, 16000, 24000, 48000],
            rate=True,
            pitch=False,       # Aura has no pitch control — say so, don't fake it
            volume=False,
            ssml=False,
            streaming=True,
            cloning=False,
            max_chars=2000,
        )

    async def list_voices(self) -> list[Voice]:
        # Prefer a static list when the vendor has few voices and no listing endpoint:
        # one fewer network call at startup, and the catalogue still works offline.
        return [
            Voice(
                provider=self.name,
                voice_id=vid,
                name=name,
                languages=["en-US"],
                gender=gender,
                cost_class=CostClass.PAID,
                tier="aura-1",
                formats=[AudioFormat.MP3, AudioFormat.WAV],
            )
            for vid, name, gender in [
                ("aura-asteria-en", "Asteria", "female"),
                ("aura-luna-en", "Luna", "female"),
                ("aura-orion-en", "Orion", "male"),
            ]
        ]

    async def synthesize(self, req: SynthRequest) -> SynthResult:
        bad = self.unsupported_for_voice(
            next((v for v in self._voices if v.voice_id == req.voice_id), None), req
        )
        if bad and req.strict:
            raise __import__(
                "ttsgrab.providers.base", fromlist=["ParamSupportError"]
            ).ParamSupportError(bad, self.name)

        params = {"model": req.voice_id, "encoding": req.format.value}
        if req.sample_rate:
            params["sample_rate"] = str(req.sample_rate)

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.deepgram.com/v1/speak",
                params=params,
                headers={"Authorization": f"Token {self.settings.deepgram.api_key}"},
                json={"text": req.text},
            )

        if resp.status_code in (401, 403):
            raise UpstreamAuthError(self.name, "Deepgram rejected the API key")
        if resp.status_code == 429:
            raise UpstreamRateLimited(self.name, "Deepgram rate limit")
        if resp.status_code >= 400:
            raise ProviderError(self.name, f"HTTP {resp.status_code}: {resp.text[:200]}")

        return SynthResult(
            audio=resp.content,
            format=req.format,
            sample_rate=req.sample_rate,
            provider=self.name,
            voice_id=req.voice_id,
            unsupported=bad,
            billed_characters=len(req.text),
            cost_micro_usd=int(len(req.text) * 15),   # $0.015/1k chars = 15 µUSD/char
            latency_ms=0,
        )
```

Then register it:

1. **`core/registry.py`** — append `"deepgram"` to `BUILTIN_MODULES`.
2. **`settings.py`** — add a `DeepgramSettings` with `enabled` / `api_key`, and a
   `deepgram: DeepgramSettings = Field(default_factory=DeepgramSettings)` field.
3. **`core/pricing.py`** — add the tier so the usage page reports cost
   (`deepgram → aura-1 → usd_per_mchar: 15.0`).
4. **Tests** — copy `tests/unit/test_providers.py::test_azure_builds_ssml_with_prosody`'s
   shape with `respx`, and assert on the request body and the `unsupported` list.

That is the whole list. Nothing in the API layer, the UI, the router or the cache needs to
change — which is the test of whether the abstraction is real.

### Rules that are not negotiable

- **Report unsupported parameters.** Fill `SynthResult.unsupported` and honour
  `req.strict`. Silently dropping `pitch` because the vendor ignores it is the one bug that
  makes a gateway useless.
- **Say what it costs.** `cost_micro_usd` must be an integer, and `billed_characters` the
  count you were actually billed for (which is not always `len(text)`).
- **Map errors to our codes.** `UpstreamAuthError` for bad credentials,
  `UpstreamRateLimited` for 429, `ProviderError` otherwise. The router's circuit breaker
  and the error envelope both depend on this.
- **Never log the key.** Pass it only in headers; the log scrubber catches common shapes
  but do not rely on it.

---

## Worker: a model that needs torch

If it needs a GPU, it belongs in a separate container — the gateway must never depend on
torch. Subclass `workers/_base/runtime.py::TTSEngine`:

```python
"""XTTS-v2 worker — personal, non-commercial use only.

⚠️  LICENCE: Coqui Public Model Licence. Non-commercial. Redistribution of the weights is
restricted. This file exists as a template for personal use; it is NOT shipped in a
container, because shipping it would put the CPML on anyone who builds this repo.
"""

from workers._base.runtime import TTSEngine, VoiceSpec, floats_to_wav, serve


class XTTSEngine(TTSEngine):
    model_name = "xtts-v2"
    license_note = "CPML — non-commercial use only"

    def __init__(self) -> None:
        super().__init__()
        self._tts = None
        self._sr = 24000

    async def load(self) -> None:
        import asyncio

        def _load():  # noqa: ANN202
            from TTS.api import TTS

            return TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(self.device)

        self._tts = await asyncio.get_running_loop().run_in_executor(None, _load)

    async def voices(self) -> list[VoiceSpec]:
        return [
            VoiceSpec(voice_id="clone", name="Zero-shot clone", languages=["en", "zh-cn"],
                      cloning=True, description="Needs reference_audio + transcript"),
        ]

    async def synthesize(self, text, voice_id, *, speed=1.0, pitch=0.0, volume=1.0,
                         style=None, reference_wav=None, reference_transcript=None):
        if reference_wav is None:
            raise ValueError("XTTS requires reference_audio")
        import asyncio, tempfile

        with tempfile.NamedTemporaryFile(suffix=".wav") as fh:
            fh.write(reference_wav)
            fh.flush()
            samples = await asyncio.get_running_loop().run_in_executor(
                None, lambda: self._tts.tts(text=text, speaker_wav=fh.name, language="zh-cn")
            )
        import numpy as np
        arr = np.asarray(samples, dtype=np.float32) * float(volume)
        return floats_to_wav(arr, self._sr), self._sr


if __name__ == "__main__":
    serve(XTTSEngine())
```

Register it — no restart of the gateway needed:

```bash
curl -X POST http://localhost:8000/v1/workers \
  -H 'Content-Type: application/json' \
  -d '{"name":"xtts","base_url":"http://192.168.1.42:8880","kind":"tts","model_hint":"xtts-v2"}'
```

The monitor health-checks it, records EWMA latency, and evicts it from routing after
`workers.unhealthy_threshold` consecutive failures. Requests then fall back along the
chain, which is what makes a worker crash a degraded experience rather than an outage.

---

## Already-existing OpenAI-shaped servers

`speaches`, `LocalAI`, `Kokoro-FastAPI`, `openedai-speech` and `fish-speech` all expose
`POST /v1/audio/speech`. Register one directly — no adapter code:

```bash
curl -X POST http://localhost:8000/v1/workers -H 'Content-Type: application/json' -d '{
  "name": "speaches", "base_url": "http://192.168.1.42:8000",
  "kind": "openai_compat", "model_hint": "kokoro"
}'
```

This is why the gateway does not embed LocalAI: a 3-line registration beats a 4 GB
sidecar, and you keep whichever server you already trust.

---

## Models deliberately not included

Researched, then excluded. The reasoning is licence-based, not quality-based.

| Model | Why not |
|---|---|
| **XTTS-v2 / Coqui** | CPML: non-commercial, and Coqui was acquired — the commercial licence path is unclear. Excellent model, unusable in a product. |
| **F5-TTS** | Code is MIT, **weights are CC-BY-NC**. The weights are the part you redistribute, so the MIT code does not help. |
| **Fish Speech** | CC-BY-NC-SA weights, plus a clause restricting hosted-service use. |
| **Piper** | The maintained fork is GPL-3.0 with per-voice licence mixes, and there is no zh-TW voice — the reason this project exists. |
| **Chatterbox** | MIT, but retired/deprecated by its authors in Aug 2026. |
| **Bert-VITS2** | AGPL-3.0. Fine for a hobby, a trap for anything closed. |
| **silero-models** | MIT and genuinely good for Russian/English; the zh voices are trained on a narrow corpus. Worth adding if you need ru. |

If your use is personal and non-commercial, all of the above work fine with the worker
template above. The point is that "it is on GitHub" is not a licence check.
