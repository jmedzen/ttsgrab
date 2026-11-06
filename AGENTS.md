# Operated by an agent? Read this, not the README.

You are talking to a **unified TTS gateway**. One API over Edge TTS, Google Cloud TTS,
Google AI Studio (Gemini), Azure Speech, and self-hosted model workers. The gateway picks a
provider, handles fallback, caches audio, and tracks cost.

Default base URL: `http://localhost:8300`

---

## The one request you probably want

```bash
curl -X POST http://localhost:8300/v1/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好，世界","voice":"zh-TW-HsiaoChenNeural"}' \
  --output out.mp3
```

Returns raw audio bytes (mp3 by default). Read the headers:

| Header | Why you care |
|---|---|
| `X-TTSGrab-Provider` | which engine actually served it |
| `X-TTSGrab-Unsupported` | parameters it ignored — **do not assume they applied** |
| `X-TTSGrab-Cached` | whether you paid for it |
| `X-TTSGrab-Cost-MicroUSD` | integer micro-USD billed |
| `X-TTSGrab-Fallback` | present only if a fallback happened |
| `X-TTSGrab-Subtitles-Url` | present when you asked for `subtitles` |
| `X-TTSGrab-Idempotent-Replay` | `true` means this was a replay, not a new charge |
| `X-TTSGrab-Degraded` | `true` when a fallback served lower quality than requested |

Every mp3/flac is also tagged (`artist`/`album`/`comment`) with the provider, voice and
character count, via a stream copy that leaves the audio bit-identical. Assume a file you
produce can be audited by whoever receives it.

---

## Decision procedure

1. **Get a voice.** `GET /v1/voices?language=zh-TW&free_only=true`. **Never invent a
   `voice_id`** — the catalogue is the only source of truth, and it changes as providers are
   enabled.
2. **Decide cost tolerance.**
   - User-facing, quality matters → `"policy": "best_quality"`
   - Bulk or background → `"policy": "cheapest"` or `"free_only": true`
   - Privacy-sensitive → `"policy": "local_only"`
3. **Synthesise.** Use `"response": "json"` when you want metadata in the same round trip.
4. **Check `unsupported`.** If something you needed was dropped, re-request with
   `"strict": true` to get a hard `422` instead of silently degraded audio.
5. **Long text?** Over ~2000 characters, use `POST /v1/jobs` and poll `GET /v1/jobs/{id}`
   rather than holding an HTTP connection open.
6. **Retrying?** Send `Idempotency-Key: <uuid>` before the first attempt. Then a network
   retry replays the stored result instead of paying again. Reusing the key with *different*
   text is a `400`, so generate a fresh UUID per logical request.
7. **Need captions?** Add `"subtitles": "srt"` (or `"vtt"`) with `"response": "json"`. The
   timings come free from the split path. The URL stays fetchable at
   `/v1/history/{id}/subtitles.srt`.
8. **Mixing providers in one project?** Set `"loudness": "podcast"` (−16 LUFS) or
   `"streaming"` (−14). Providers differ by several dB; unnormalised cuts are audible as a
   volume jump between sentences.

---

## Rules

- **Never invent a voice id.** Query `/v1/voices` first, and cache the result in your own
  context — it changes rarely.
- **Read `error.retryable`.** `false` means a retry cannot succeed. Retrying
  `budget_exceeded`, `unsupported_param` or `upstream_auth_error` burns time and money for
  nothing.
- **On `all_providers_failed`,** try `error.suggested_fallback`. If that also fails, surface
  the failure. Do not silently produce silence.
- **Check `official` in `/v1/providers`.** `false` means an unofficial interface that may
  break without notice and whose terms may not permit automated use. Do not send personal
  data to those.
- **Do not exceed the budget.** `402 budget_exceeded` is a hard stop by design, not a
  transient error.
- **Reuse cached audio.** Identical text + voice + params is served from disk at zero cost.
  Do not add timestamps or random suffixes to force a fresh synthesis unless you mean it.
- **A `429` carries `Retry-After` in seconds.** Honour it. Retrying sooner cannot succeed.
- **Google AI Studio free keys are rate-limited per day, not per minute.** A free key allows
  only ~10 TTS requests per day; a long document will exhaust it in one go. If
  `/v1/providers` reports `google_ai_studio` as enabled but synthesis returns
  `quota_exceeded`, you have hit that daily cap — fall back rather than retrying, because
  retrying cannot help until the next Pacific midnight.

---

## Endpoints you will use

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/tts` | Synthesise. Returns audio bytes (or JSON with `"response":"json"`) |
| POST | `/v1/tts/stream` | Chunked audio; `?stream_format=sse` for SSE |
| POST | `/v1/tts/estimate` | Duration + cost, no synthesis, no charge |
| GET | `/v1/voices` | The catalogue. Filter by `language`, `provider`, `free_only` |
| GET | `/v1/providers` | Health, cost class, `official` flag, `stability` |
| GET | `/v1/capabilities` | Formats, sample rates, policies, limits, and the running build |
| POST | `/v1/compare` | Same text across up to 8 targets, with metrics |
| POST | `/v1/audio/speech` | OpenAI-compatible; change only `base_url` in the SDK |
| POST | `/v1/jobs` | Async job with optional `webhook_url` |
| GET | `/v1/jobs/{id}` | Poll it |
| GET | `/v1/usage` | This month's characters and cost per provider |
| GET | `/v1/history` | Past syntheses, replayable |
| GET/POST/DELETE | `/v1/providers/google_ai_studio/keys` | **Admin only.** Manage free/paid Google AI Studio keys |
| — | `/mcp` | MCP tools: `synthesize_speech`, `list_voices`, `list_providers`, `get_usage` |
| GET | `/healthz` | Liveness **and the build number** — quote it in bug reports |
| — | `/llms.txt`, `/agents.md` | This document, machine-readable |

---

## Errors

```json
{"error": {"code": "unsupported_param", "message": "…", "retryable": false,
           "request_id": "…", "suggested_fallback": ["edge"]}}
```

Branch on `code`, never on `message` — messages are for humans and may change.

| Code | Retryable | What to do |
|---|---|---|
| `invalid_request` | no | Fix the request |
| `unauthorized` / `forbidden` | no | Fix the credential or allowlist |
| `not_found` | no | Re-fetch the catalogue |
| `text_too_long` | no | Split the text, or use `/v1/jobs` |
| `request_too_large` | no | The body exceeded the operator's limit; send less |
| `unsupported_param` | no | Drop the parameter or try another provider |
| `rate_limited` | **yes** | Back off; honour `Retry-After` |
| `budget_exceeded` | no | Stop. Tell the human |
| `quota_exceeded` | no | Stop. This key is out of characters |
| `provider_error` / `provider_unavailable` | **yes** | Retry with backoff |
| `upstream_auth_error` | no | The operator's key is bad. Tell a human |
| `all_providers_failed` | **yes** | Try `suggested_fallback`, then give up loudly |
| `timeout` | **yes** | Retry, or use `/v1/jobs` |

---

## Traditional Chinese

Local models and most cloud engines are trained on Mandarin (zh-CN) lexicons. The gateway
normalises Traditional → Simplified with OpenCC and applies a Taiwan pronunciation
dictionary (`垃圾`→`拉基`, `企業`→`起業`) before synthesis, but Taiwan-specific readings can
still differ.

For production zh-TW, prefer `edge` (`zh-TW-*Neural`) or an Azure `zh-TW-*` voice. Chirp 3 HD
has **no** zh-TW voice. Kokoro's Mandarin is weak — its own authors grade it "D".

---

## MCP

If you are an MCP client, mount `http://localhost:8300/mcp/` and use:

- `synthesize_speech({text, voice, format?, speed?, provider?, policy?})`
- `list_voices({language?, provider?, free_only?, limit?})`
- `list_providers({})`
- `get_usage({})`

Call `list_voices` before `synthesize_speech`. Every time — the catalogue depends on which
credentials the operator has configured.
