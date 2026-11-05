# API reference

Base URL `http://localhost:8000` by default. Interactive docs at `/docs`, ReDoc at
`/redoc`, raw schema at `/openapi.json`.

Everything below is also reachable through the OpenAI-compatible endpoint and MCP — they
share one pipeline, so caching, budgets and history behave identically.

---

## Authentication

Two kinds of caller:

| Caller | Credential | Can do |
|---|---|---|
| **Admin** (you, in the browser) | `TTSGRAB_ADMIN_TOKEN` in `Authorization: Bearer …` or `X-API-Key` | everything, including key management, workers, references, audit |
| **API key** (an agent, a service) | `ttsg_…` token, created at `/v1/keys` | synthesis and reads allowed by its allowlist |

```bash
# admin
curl -H "Authorization: Bearer $TTSGRAB_ADMIN_TOKEN" localhost:8000/v1/usage

# api key
curl -H "Authorization: Bearer ttsg_abc123…" localhost:8000/v1/tts -d '…'
```

When **no** admin token is configured and the request comes from `127.0.0.1`/`::1`,
requests are allowed unauthenticated. That is for local development only; setting
`TTSGRAB_SECURITY__ALLOW_LOCALHOST_WITHOUT_TOKEN=false` disables it, and any request from a
non-local address is always rejected without a token.

An API key carries:

- `provider_allowlist` — empty means "all providers"; otherwise a forced provider outside
  the list is a `403`.
- `rate_limit_per_minute` — sliding window, returns `429` with `Retry-After`.
- `monthly_char_quota` — `402 quota_exceeded` once exceeded this calendar month (UTC).

---

## Errors

Every failure has the same shape, at every layer:

```json
{
  "error": {
    "code": "unsupported_param",
    "message": "google_cloud cannot honour: speed, ssml (voice en-US-Chirp3-HD-Kore)",
    "retryable": false,
    "request_id": "3f9c…",
    "details": {"provider": "google_cloud", "params": ["speed", "ssml"]},
    "suggested_fallback": ["edge", "azure"]
  }
}
```

Branch on `code`, never on `message`.

| Code | HTTP | Retryable | Meaning |
|---|---|---|---|
| `invalid_request` | 400 | no | Malformed or contradictory input |
| `unauthorized` | 401 | no | Missing or bad credential |
| `forbidden` | 403 | no | Credential valid but not permitted (allowlist, SSRF block) |
| `not_found` | 404 | no | Unknown voice, job, worker, history id |
| `text_too_long` | 413 | no | Over `security.max_text_chars` |
| `request_too_large` | 413 | no | Over `security.max_request_body_bytes` — the body was refused unread |
| `unsupported_param` | 422 | no | `strict: true` and the provider cannot honour a parameter |
| `rate_limited` | 429 | **yes** | Slow down; `Retry-After` is set |
| `budget_exceeded` | 402 | no | Monthly budget cap hit. Fix the budget, then retry |
| `quota_exceeded` | 402 | no | This API key's monthly characters are spent |
| `provider_error` | 502 | **yes** | Upstream failed |
| `provider_unavailable` | 503 | **yes** | Upstream unreachable or disabled |
| `upstream_auth_error` | 502 | no | **Your** key was rejected. Retrying makes it worse |
| `upstream_rate_limited` | 429 | **yes** | Upstream throttled us |
| `all_providers_failed` | 503 | **yes** | Every candidate failed; see `details.attempts` |
| `timeout` | 504 | **yes** | Upstream took too long |
| `internal_error` | 500 | **yes** | A bug. `request_id` finds it in the logs |

`retryable: false` means a retry cannot succeed — do not burn time or money on it.

---

## `POST /v1/tts`

The call you want 90% of the time.

```json
{
  "text": "你好，世界",
  "voice": "zh-TW-HsiaoChenNeural",
  "format": "mp3",
  "speed": 1.0,
  "pitch": 0.0,
  "volume": 1.0,
  "sample_rate": null,
  "provider": null,
  "policy": "cheapest",
  "strict": false,
  "bypass_cache": false,
  "response": "audio"
}
```

| Field | Default | Notes |
|---|---|---|
| `text` | required | Capped by `security.max_text_chars`; longer text is split into sentences and synthesised in parallel |
| `voice` / `voice_id` | required | A `voice_id` from `/v1/voices`. Both spellings accepted |
| `format` | `mp3` | `mp3` · `wav` · `opus` · `flac`. Unsupported values are transcoded and reported |
| `sample_rate` | provider default | Transcodes if needed |
| `speed` | `1.0` | `0.25`–`4.0`. Providers with a narrower range get the closest supported value, reported in `unsupported` |
| `pitch` | `0.0` | Semitones, `-12`–`12` |
| `volume` | `1.0` | `0`–`2` |
| `provider` | auto | Force one. Bypasses policy ranking but not the budget check |
| `policy` | router default | `cheapest` · `fastest` · `best_quality` · `local_only` · `free_only` · `explicit` |
| `strict` | `false` | `true` turns any unsupported parameter into `422` |
| `bypass_cache` | `false` | Force a fresh synthesis |
| `response` | `audio` | `json` returns base64 audio plus full metadata |

**Response** — `200` with the audio bytes and these headers:

| Header | Example | Meaning |
|---|---|---|
| `X-TTSGrab-Provider` | `edge` | Who actually served it |
| `X-TTSGrab-Voice` | `zh-TW-HsiaoChenNeural` | Voice used |
| `X-TTSGrab-Format` | `mp3` | Format returned (after any transcode) |
| `X-TTSGrab-Cached` | `true` | Served from disk cache |
| `X-TTSGrab-Cost-MicroUSD` | `0` | Integer micro-USD billed |
| `X-TTSGrab-Latency-Ms` | `1306` | Upstream latency |
| `X-TTSGrab-Unsupported` | `volume,ssml` | Parameters the provider ignored — **read this** |
| `X-TTSGrab-Fallback` | `stub->edge` | Present only when a fallback happened |
| `X-TTSGrab-History-Id` | `3f9c…` | Replay or delete via `/v1/history` |

With `"response": "json"`:

```json
{
  "provider": "edge",
  "voice_id": "zh-TW-HsiaoChenNeural",
  "format": "mp3",
  "audio_base64": "SUQzBAAAA…",
  "bytes": 22032,
  "duration_ms": 2880,
  "characters": 6,
  "cost_micro_usd": 0,
  "cost_usd": 0.0,
  "cached": false,
  "unsupported": [],
  "transcoded": [],
  "history_id": "3f9c…"
}
```

### Long text

Above `longform.chunk_threshold` characters the text is split on sentence boundaries,
Traditional Chinese is normalised to Simplified plus a Taiwan pronunciation dictionary
(`enterprise` → `起業`, `garbage` → `拉基`), chunks are synthesised concurrently up to
`longform.max_concurrency`, then joined with ffmpeg. The response is a single file; nothing
about the splitting is visible to the caller except the latency.

---

## `POST /v1/tts/stream`

Same body. Returns chunked audio as it is produced.

```bash
curl -N -X POST localhost:8000/v1/tts/stream \
  -H 'Content-Type: application/json' \
  -d '{"text":"串流測試","voice":"zh-TW-HsiaoChenNeural","format":"mp3"}' \
  --output stream.mp3
```

Add `?stream_format=sse` for Server-Sent Events, which is what browsers need:

```
event: meta
data: {"provider":"edge","voice_id":"…","format":"mp3"}

event: audio
data: {"chunk_base64":"SUQz…","index":0}

event: done
data: {"bytes":22032,"duration_ms":2880}
```

Long text streams chunk-by-chunk as each sentence finishes, so playback starts before the
last sentence has been synthesised.

---

## `GET /v1/voices`

```bash
curl 'localhost:8000/v1/voices?language=zh-TW&free_only=true&q=hsiao&limit=10'
```

| Query | Meaning |
|---|---|
| `q` | Substring match on voice id and display name |
| `provider` | Repeatable |
| `language` | Prefix match: `zh`, `zh-TW`, `en-US` |
| `gender` | `male` · `female` · `unknown` |
| `free_only` | `cost_class == free` |
| `cloneable` | Only voices that accept a reference clip |
| `streamable` | Only voices with streaming support |
| `limit` / `offset` | Pagination |

```json
{
  "total": 322, "count": 3,
  "voices": [{
    "provider": "edge", "voice_id": "zh-TW-HsiaoChenNeural",
    "name": "HsiaoChen - Chinese (Taiwan)", "languages": ["zh-TW"],
    "gender": "female", "styles": [], "cost_class": "free",
    "tier": "neural", "streaming": true, "cloning": false, "ssml": false,
    "price_usd_per_mchar": 0.0, "free_chars_per_month": 0
  }],
  "languages": ["zh-TW"], "providers": ["edge"]
}
```

`total` is the count before `limit`, so you can paginate without a second call.

---

## `GET /v1/providers`

```json
{
  "providers": [{
    "name": "google_cloud", "enabled": false, "kind": "cloud",
    "official": true, "stability": "stable", "cost_class": "free_tier",
    "reason": "google_cloud.api_key is not set",
    "voices": 0, "up": null, "latency_ms": null,
    "note": "Google Cloud Text-to-Speech", "tos_url": "https://cloud.google.com/terms"
  }]
}
```

`enabled: false` is normal, not an error — it means no credentials. `up` is `null` before
the first health check. `official: false` marks an unofficial interface; `stability:
fragile` means it has broken before and will again.

## `POST /v1/providers/{name}/refresh`

Re-fetches a provider's voice list. Useful after enabling a provider, or when a stale
catalogue is hiding a voice you know exists.

```bash
curl -X POST localhost:8000/v1/providers/edge/refresh -H "Authorization: Bearer $TOKEN"
```

```json
{"name": "edge", "enabled": true, "reason": null, "voices": 322, "reloaded": true}
```

An unreachable upstream is reported as an error and the **previously loaded** adapter stays
in use, so a failed refresh never leaves you with zero voices. Refreshing a provider that is
disabled returns `voices: 0` and `reloaded: false` rather than pretending to succeed.

---

## `GET /v1/capabilities`

What this deployment can actually do right now — read it instead of hard-coding assumptions.

```json
{
  "formats": ["mp3", "wav", "opus", "flac"],
  "sample_rates": [8000, 16000, 22050, 24000, 44100, 48000],
  "policies": ["cheapest", "fastest", "best_quality", "local_only", "free_only"],
  "max_text_chars": 20000,
  "streaming": true,
  "cloning": true,
  "providers": ["edge"]
}
```

## `GET /v1/models`

OpenAI-shaped model list, so a stock OpenAI client that probes for models does not fail.
The entries are aliases for voices, not real model ids.

```json
{"object": "list", "data": [{"id": "tts-1", "object": "model", "owned_by": "ttsgrab"}]}
```

---

## Operations

Unauthenticated by design: these are what a container orchestrator and a Prometheus scrape
need.

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness. Returns 200 as soon as the process can answer. |
| `GET /readyz` | Readiness. Checks the database and that at least one provider loaded. 503 while starting. |
| `GET /metrics` | Prometheus text format: request counts, latency histograms, cache hits, provider up/down. |

`/healthz` answers the question you actually ask during an incident:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "build": 19,
  "commit": "e256820",
  "dirty": false,
  "built_at": "2026-11-05T14:58:44Z"
}
```

`version` is the semantic version and moves rarely — every commit since the last bump
reports the same one. `build` increments on **every commit** (it is the git commit count),
which is what distinguishes "the build with the fix" from "the build without it". `commit`
is the short SHA, and `dirty: true` means the tree it was built from had uncommitted
changes — so the same build number does not necessarily mean the same code.

`build` is `null` when nothing supplied it: a plain source checkout that never ran
`make build-info`, or an image built without the build-arg. Treat null as "unknown", and
never as zero.

`/readyz` is a different question:

```json
{"status": "ready", "checks": {"database": "ok", "providers": "ok",
 "providers_enabled": ["edge"], "voices": 322}}
```

---

## `POST /v1/compare`

```json
{
  "text": "同一句話，不同聲音。",
  "format": "mp3",
  "targets": [
    {"provider": "edge", "voice": "zh-TW-HsiaoChenNeural"},
    {"provider": "edge", "voice": "zh-TW-YunJheNeural"},
    {"provider": "azure", "voice": "zh-TW-HsiaoChenNeural"}
  ]
}
```

Runs all targets concurrently (max 8) and returns per-target metrics. One failing target
becomes an error row rather than failing the request:

```json
{
  "text_chars": 10,
  "results": [{
    "provider": "edge", "voice_id": "zh-TW-HsiaoChenNeural",
    "history_id": "…", "audio_url": "/v1/history/…/audio",
    "format": "mp3", "bytes": 11520, "duration_ms": 2880,
    "latency_ms": 1180, "first_byte_ms": 970, "rtf": 0.41,
    "characters": 10, "cost_micro_usd": 0, "cost_usd": 0.0,
    "cached": false, "unsupported": []
  }]
}
```

`rtf` is real-time factor: below 1.0 means faster than playback — 0.41 means a 2.9 s clip
took 1.2 s to produce.

`first_byte_ms` is `null` for a provider that cannot stream. It is the number that decides
whether an interface feels instant, so treat a null as "unknown", not as "zero".

---

## `POST /v1/tts/estimate`

No synthesis, no charge. Same body as `/v1/tts`.

```json
{
  "characters": 14, "estimated_duration_ms": 2888,
  "provider": "edge", "tier": "neural",
  "estimated_cost_micro_usd": 0, "estimated_cost_usd": 0.0,
  "within_free_tier": true
}
```

Duration is a heuristic (CJK characters ≈ 150 ms, Latin words ≈ 350 ms) divided by speed.
Cost is exact, because it comes from the same pricing table the ledger uses.

---

## Jobs

For text too long to sit on one HTTP connection, or when you want a webhook.

```bash
curl -X POST localhost:8000/v1/jobs -H 'Content-Type: application/json' -d '{
  "text": "很長的文章…",
  "voice": "zh-TW-HsiaoChenNeural",
  "webhook_url": "https://example.com/hooks/tts"
}'
```

```json
{"id": "a1b2…", "status": "queued", "progress": 0.0, "result": null, "error": null}
```

Poll `GET /v1/jobs/{job_id}`; `status` moves `queued` → `running` → `succeeded` | `failed`.
On completion the webhook receives:

```json
{"job_id": "a1b2…", "status": "succeeded",
 "result": {"provider": "edge", "audio_url": "/v1/history/…/audio", "duration_ms": 41200}}
```

The job table is queue-agnostic, so moving to Redis/RQ later does not change this API.

---

## `POST /v1/audio/speech` (OpenAI-compatible)

Accepts OpenAI's body and returns raw audio, so an unchanged SDK works with only `base_url`
changed.

```python
client = OpenAI(base_url="http://localhost:8000/v1", api_key="ttsg_…")
client.audio.speech.create(model="tts-1", voice="nova", input="Hello", response_format="mp3")
```

- `voice` may be an OpenAI alias (`alloy`, `echo`, `fable`, `onyx`, `nova`, `shimmer`) **or**
  a real `voice_id`. Aliases are mapped per language — "alloy" resolves to a zh-TW voice
  when the text is Chinese.
- `model` is accepted and echoed; it does not change routing.
- `instructions` maps to our `prompt` field.
- Errors use OpenAI's `{"error": {"message", "type", "code"}}` shape so SDK error handling
  keeps working.

If the container you asked for cannot be produced, `X-TTSGrab-Format-Substituted` says what
you got instead of lying.

---

## History, usage, stats

| Endpoint | Returns |
|---|---|
| `GET /v1/history?q=&limit=&offset=` | Past syntheses: text, params, provider, latency, characters, cost, audio URL |
| `GET /v1/history/{record_id}/audio` | The audio, as stored |
| `DELETE /v1/history/{record_id}` | Removes the row and the file |
| `GET /v1/usage?days=30` | Month totals, per-provider breakdown, free-tier status, budget status |
| `GET /v1/usage/series?days=30` | `[{date, characters, requests, cost_micro_usd}]` |
| `GET /v1/usage/free-tier` | Remaining free characters per provider/tier |
| `GET /v1/stats` | Dashboard aggregates: counts, cache hit rate, top voices |

---

## Workers

```bash
# register
curl -X POST localhost:8000/v1/workers -H 'Authorization: Bearer '$TOKEN \
  -H 'Content-Type: application/json' \
  -d '{"name":"gpu-box","base_url":"http://192.168.1.42:8880","kind":"tts"}'

# check now, rather than waiting for the monitor
curl -X POST localhost:8000/v1/workers/{worker_id}/check -H 'Authorization: Bearer '$TOKEN

# remove
curl -X DELETE localhost:8000/v1/workers/{worker_id} -H 'Authorization: Bearer '$TOKEN
```

`base_url` is SSRF-checked: only `http`/`https`, link-local and cloud-metadata addresses
(`169.254.0.0/16`, `fd00:ec2::254`, …) are always refused. Private LAN ranges are allowed by
default and can be restricted with `security.allowed_worker_hosts`.

`kind` is `tts` (our three-endpoint protocol) or `openai_compat` (any server exposing
`/v1/audio/speech`).

---

## References (voice cloning)

```bash
curl -X POST localhost:8000/v1/references \
  -H 'Authorization: Bearer '$TOKEN \
  -F 'file=@my-voice.wav' \
  -F 'name=my-voice' \
  -F 'provider=gpt_sovits' \
  -F 'transcript=這是我錄的一句話' \
  -F 'consent_note=my own voice'
```

`consent_note` is **required**, deliberately. Cloning a voice you do not have rights to is
illegal in the EU (AI Act Art. 50) and under Taiwan's Personal Data Protection Act; making
the field mandatory is a speed bump, not a solution.

The upload is validated by magic bytes (not by the declared MIME type), size-capped, and
stored under `data/reference/`. Use the `id` as `reference_audio_id` in `/v1/tts`.

---

## API keys and audit

```bash
curl -X POST localhost:8000/v1/keys -H 'Authorization: Bearer '$TOKEN \
  -H 'Content-Type: application/json' -d '{
    "name": "my-agent",
    "provider_allowlist": ["edge"],
    "rate_limit_per_minute": 30,
    "monthly_char_quota": 50000
  }'
```

```json
{"id": "…", "name": "my-agent", "prefix": "ttsg_ab12", "token": "ttsg_ab12…"}
```

**The `token` is returned once and never again** — only its SHA-256 is stored. Revoke with
`DELETE /v1/keys/{key_id}`. `GET /v1/audit` shows who did what.

---

## MCP

Mounted at `/mcp` (streamable HTTP) when the `mcp` extra is installed.

```json
{
  "mcpServers": {
    "ttsgrab": {
      "url": "http://localhost:8000/mcp/",
      "headers": {"Authorization": "Bearer ttsg_…"}
    }
  }
}
```

Tools: `synthesize_speech` · `list_voices` · `list_providers` · `get_usage`.

The trailing slash matters for some clients — the gateway normalises `POST /mcp` to
`/mcp/`, but a client that resolves paths itself may not.

Machine-readable orientation lives at `/llms.txt` and `/agents.md`.
