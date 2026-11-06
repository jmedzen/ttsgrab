# Provider comparison

Everything here comes from vendor documentation read at the time of writing, plus
measurements taken on this machine where measurement was possible. Where it was not, the
cell says so — a guessed latency number is worse than an empty one, because someone will
plan around it.

Last reviewed: 2026-11. Vendor pricing and free tiers change; re-check before committing.

---

## The table

| Provider | Official | Price / 1M chars | Free allowance | zh-TW | Latency (measured) | Stability | Interface kind |
|---|---|---|---|---|---|---|---|
| **Edge TTS** | ✗ | free | unlimited, unofficial | ✓ 3 voices | **~650 ms** total, **~440 ms** first byte | **fragile** — 6 outages since 2024 | unofficial endpoint |
| **Google Cloud TTS** | ✓ | $4 (Standard) – $160 (Studio) | 4M Std+WaveNet, 1M Neural2 / Chirp3-HD / Studio / Polyglot | ✓ `cmn-TW-*`; **not** in Chirp 3 HD | not measured — no account | stable | REST, service account |
| **Google AI Studio** | ✓ | ~$12 | 10 requests/day per free key | ✓ | not measured — no account | preview | REST, API key |
| **Azure Speech** | ✓ | $16 (neural) | 500k on F0, **fails closed** | ✓ widest zh-TW coverage | not measured — no account | stable | REST, subscription key |
| **Kokoro** | self-hosted | electricity | — | ✗ zh-CN only, weak | not measured — no GPU here | stable (Apache-2.0) | worker container |
| **CosyVoice2** | self-hosted | electricity | — | ✓ strong, cloneable | not measured — no GPU here | stable (Apache-2.0) | worker container |
| **GPT-SoVITS** | self-hosted | electricity | — | ✓ strong, cloneable | not measured — no GPU here | stable (MIT) | worker container |
| **gTTS** | ✗ | free | unlimited, ToS-ambiguous | ✓ | not measured | fragile | unofficial endpoint |
| **translate_tts** | ✗ | free | unlimited, rate-limited | ✓ | not measured | fragile | unofficial endpoint |

"Latency" is one short sentence, warm connection, audio size under 20 KB. It is not a
benchmark of the vendor's datacentre — it is what one laptop saw, which is the number that
matters for interactive use.

---

## Quality

| | Verdict |
|---|---|
| **Google Chirp 3 HD / Studio** | Best naturalness of the paid options. Chirp 3 HD has no zh-TW voice — a real limitation if that is your language. |
| **Azure neural** | Very close to Google, and the best zh-TW coverage of any provider here. This is the safe production choice for Chinese. |
| **Edge** | The same neural engine family Azure uses, which is why it sounds better than its price suggests. Do not build a business on it. |
| **CosyVoice2 / GPT-SoVITS** | Comparable to paid neural on Mandarin when the speaker is in their training data; noticeably worse outside it. Both clone well. |
| **Kokoro** | Excellent for its size in English. Its own authors grade the Mandarin "D". Do not use it for zh-TW. |

The honest summary: for zh-TW production, Azure. For zh-TW on a budget, Edge. For English
with privacy, CosyVoice2 or GPT-SoVITS on a local GPU.

---

## Cost, worked out

Google's tiering is the part people get wrong. At list prices:

| Volume | Cheapest credible choice | Why |
|---|---|---|
| 1M chars/month, zh-TW | Edge | Free, and quality is sufficient for most UI |
| 1M chars/month, need an SLA | Azure F0 → $16 | F0 fails closed at 500k, so there is no surprise bill |
| 4M chars/month | Google Standard | 4M free, then $4/1M — the cheapest paid tier by a wide margin |
| 4M chars/month, zh-TW | Google WaveNet | Same free allowance; zh-TW lives in WaveNet/Neural2, not Chirp 3 HD |
| 20M chars/month | Self-host, or negotiate | $80+/month on Google; a second-hand GPU pays for itself |

The gateway encodes exactly one opinion about this: **a budget breach is refused, not
downgraded** (`402 budget_exceeded`). Google overage bills silently by default; Azure F0
does not. Configure both.

---

## Legal and terms

| Provider | What the terms actually say |
|---|---|
| **Edge TTS** | No supported API exists. The endpoint is the browser's read-aloud service, gated by a rotating `Sec-MS-GEC` token. Microsoft grants no redistribution right for the output. Personal and development use only. |
| **gTTS / translate_tts** | Scrape `translate.google.com`. Google's ToS prohibits automated access. Rate-limited and blocked at any real volume. |
| **Google Cloud TTS** | Commercial use permitted under the Cloud terms. Audio may be used in your product; the service account key is a credential and must be treated as one. |
| **Google AI Studio** | Gemini 3.8 Flash TTS. Free keys are capped at a few requests per **day** (10 RPD as of Nov 2026), so a long document will exhaust them; add a billed key. |
| **Azure Speech** | Commercial use permitted. There is a documented policy against using it to impersonate; voice cloning requires consent. |
| **Kokoro / CosyVoice2 / GPT-SoVITS** | Code **and** weights are permissive (Apache-2.0, Apache-2.0, MIT). This is why they are the three that ship. |

Excluded for licence reasons, documented rather than shipped: XTTS-v2 (CPML, non-commercial),
F5-TTS (CC-BY-NC weights), Fish Speech (NC plus a hosted-service clause), Piper (GPL-3.0 fork
with per-voice mixed licences, and no zh-TW).

---

## What this gateway does with that

- **Unofficial providers are off by default** and show a warning in the UI. Edge is the
  single exception, by an explicit decision that a keyless default is worth the fragility.
- **A paid provider with no credentials is marked `enabled: false` with a reason**, not an
  error. Missing keys never crash startup.
- **`GET /v1/providers` reports `official`, `stability` and `cost_class` per provider**, so a
  caller can decide for itself rather than trusting this document.
- **Budgets are per provider and refused on breach.** There is no global dollar cap, because
  "spend $3 on Google Cloud and nothing on Google AI Studio" is not one number.

---

## Reproducing the measurements

```bash
# Edge only: it is the one that needs no credentials.
uv run python - <<'PY'
import asyncio, time
from ttsgrab.settings import load_settings
from ttsgrab.core.context import build_context
from ttsgrab.core.types import AudioFormat, SynthRequest

async def main():
    settings = load_settings("config.yaml")
    settings.cache.enabled = False
    ctx = await build_context(settings)
    provider = ctx.registry.get("edge")
    req = SynthRequest(text="Latency measurement sentence.",
                       voice_id="en-US-AriaNeural", format=AudioFormat.MP3)
    await provider.synthesize(req)          # warm the TLS connection
    for _ in range(3):
        started = time.perf_counter()
        result = await provider.synthesize(req)
        print(f"{(time.perf_counter() - started) * 1000:.0f} ms, "
              f"first byte {result.first_byte_ms} ms")
    await ctx.registry.shutdown_all()

asyncio.run(main())
PY
```

For the paid providers, add a key to your `.env`, then compare all of them at once with
`POST /v1/compare` or the **Compare** page in the UI.
