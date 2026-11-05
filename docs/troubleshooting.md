# Troubleshooting

Failures you will actually hit, in rough order of frequency. Each entry says how to confirm
the diagnosis before trying the fix — guessing wastes more time than it saves.

---

## "NoAudioReceived" / empty audio from Edge

**Symptom**

```
all_providers_failed: edge: NoAudioReceived
```

**Confirm**

```bash
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' -X POST localhost:8000/v1/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"test","voice":"en-US-AriaNeural"}'
```

Run it three times. If it sometimes returns 200 with bytes, it is the upstream service being
flaky, not your setup.

**Why** — Edge TTS is not an official API. The endpoint requires a `Sec-MS-GEC` token that
Microsoft rotates, and it returns an empty stream (HTTP 200, zero audio) when it does not
like the token or is under load. It has broken for days at a time: 2024-10, 2024-11,
2025-08, 2025-12, 2026-01, 2026-10.

**Fix** — nothing on your side. The adapter already retries 4× with backoff
(`providers/edge.py:MAX_ATTEMPTS`). If it is persistently down:

1. Upgrade `edge-tts` — token-generation fixes land there: `pip install -U edge-tts`.
2. Point `router.fallback_chain` at a provider you control.
3. For anything that matters, configure Google or Azure.

This is the price of a free unofficial provider. It is why the UI labels it `unofficial`.

---

## Provider is enabled but shows 0 voices

**Confirm**

```bash
curl -s localhost:8000/v1/providers | python3 -m json.tool
```

Look at `enabled`, `reason`, and `voices`.

**Causes, in order**

1. **Credentials missing.** `reason` says exactly which key. Google accepts either an API
   key or a service-account file; if you set `GOOGLE_CREDENTIALS_FILE` in Docker, confirm the
   file is actually mounted:
   ```bash
   docker compose exec gateway ls -l /run/secrets/google-credentials.json
   ```
2. **Credential in the wrong env var.** The nesting matters:
   `TTSGRAB_GOOGLE_CLOUD__API_KEY` (double underscore), not `TTSGRAB_GOOGLE_API_KEY`.
3. **The voice-list call failed.** `docker compose logs gateway | grep -i voices` — a 403
   here usually means the API is not enabled on the Google Cloud project, or the service
   account lacks `cloudtexttospeech.voices.read`.
4. **Billing not enabled** on the Google project. The Cloud TTS API refuses to list voices
   for a project without a billing account, even inside the free tier.

Refresh without restarting:

```bash
curl -X POST localhost:8000/v1/providers/google_cloud/refresh -H "Authorization: Bearer $TOKEN"
```

---

## `402 budget_exceeded`

This is the feature working.

```json
{"error": {"code": "budget_exceeded", "retryable": false,
           "message": "monthly budget of $5.00 reached ($5.03 used)"}}
```

**Check the real number**

```bash
curl -s localhost:8000/v1/usage -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

**Why it refuses instead of degrading** — falling back to a free provider would silently
change the voice quality. A hard stop is loud and fixable; a quiet downgrade is not.

**Fix** — raise `TTSGRAB_MONTHLY_BUDGET_USD`, or set it to `0` for no cap (not recommended
with Google, whose overage is auto-billed and fails open).

Note the asymmetry: **Azure F0 fails closed** (429, no charge), **Google fails open** (bills
you). If you want a hard guarantee regardless of the gateway's own accounting, use Azure F0,
or set a budget in the Google Cloud console as well — two independent caps.

---

## Free tier math does not match the Google bill

**Confirm**

```bash
curl -s 'localhost:8000/v1/usage/free-tier' -H "Authorization: Bearer $TOKEN"
```

This reports per-tier usage for the current UTC month, from our own ledger.

**Known sources of difference**

1. **Timezone.** Our month boundary is UTC; Google's billing cycle may differ.
2. **Tier guessing.** Tiers are inferred from the voice id (`cmn-TW-Wavenet-A` → WaveNet).
   A voice id Google invents later falls back to `Standard` pricing.
3. **SSML markup is billable.** We bill `len(text)`, which matches Google for plain text.
   If you send raw SSML through, the tags count too.
4. **Chars synthesised outside this gateway** are invisible to us.

Our ledger is for *preventing* overspend, not for accounting. Reconcile against the Google
Cloud billing report when it matters.

---

## Streaming buffers everything and returns at the end

**Cause** — almost always the reverse proxy, not the gateway.

**Fix** — disable response buffering:

- Caddy: `flush_interval -1` inside the `reverse_proxy` block.
- nginx: `proxy_buffering off;` and `proxy_cache off;` for the location.
- Cloudflare: streaming works, but it buffers ~100 KB before flushing; for short clips that
  means no early audio.

Confirm the gateway is streaming by hitting it directly, bypassing the proxy:

```bash
curl -N -X POST http://127.0.0.1:8000/v1/tts/stream \
  -H 'Content-Type: application/json' \
  -d '{"text":"串流測試。這是第二句。這是第三句。","voice":"zh-TW-HsiaoChenNeural"}'
```

If chunks arrive progressively here but not through the proxy, it is the proxy.

---

## OpenAI SDK returns "audio file appears to be corrupt"

**Confirm**

```bash
curl -s -D - -o /tmp/x.mp3 -X POST localhost:8000/v1/audio/speech \
  -H 'Content-Type: application/json' \
  -d '{"model":"tts-1","input":"hello","voice":"alloy","response_format":"mp3"}' | head -20
```

Check `content-type` and `content-length`.

**Causes**

1. **Alias not resolving.** `alloy` maps to a real voice id; if the catalogue is empty at
   startup (network failure), the mapping has nothing to resolve to. Check `GET /v1/voices`.
2. **`response_format: "aac"`.** We have no AAC encoder; you get mp3 plus
   `X-TTSGrab-Format-Substituted`. Use `mp3`.
3. **The SDK wrote an error body to disk** because it does not raise on the JSON error
   envelope. Check the file starts with `ID3` (mp3) or `RIFF` (wav), not `{`.

---

## Worker registered but never used

**Confirm**

```bash
curl -s localhost:8000/v1/workers -H "Authorization: Bearer $TOKEN" | python3 -m json.tool
```

Look at `healthy`, `last_error`, `latency_ms_ewma`.

**Causes**

1. **Unhealthy.** `last_error` has the reason. From the gateway container:
   ```bash
   docker compose exec gateway curl -v http://192.168.1.42:8880/health
   ```
   A timeout here is a firewall or a wrong IP, not a gateway bug.
2. **Routing policy excludes it.** `local_only` requires the provider name to be in
   `LOCAL_PROVIDERS` (`kokoro`, `gpt_sovits`, `cosyvoice`, `piper`). A worker named
   `my-gpu-box` will never match `local_only`. Name it after the model.
3. **The voice does not exist on it.** Routing only considers providers that actually have
   the requested `voice_id`.
4. **Circuit breaker open.** It stays open for `router.circuit_breaker_cooldown_s` after failures,
   then half-opens with one probe request. Force a check:
   ```bash
   curl -X POST localhost:8000/v1/workers/{id}/check -H "Authorization: Bearer $TOKEN"
   ```

---

## Forward-fallback works but the voice changes

Expected, and reported: `X-TTSGrab-Fallback: gpu-box->edge`. Edge's `zh-TW-HsiaoChenNeural`
and CosyVoice's clone are not the same voice, obviously.

If you need the voice to be stable, either:

- set `"allow_fallback": false` and handle the failure, or
- use a provider that exposes the same voice id (`edge` and `azure` both have
  `zh-TW-HsiaoChenNeural`), so the fallback is acoustically close.

---

## `unsupported: ["speed"]` on a provider that has speed

**Cause** — the adapter's `Capabilities` says `rate=False`, or the voice overrides it.
Chirp 3 HD genuinely has no speed control; Cloud TTS Neural2 does.

**Confirm** — `GET /v1/capabilities`, then check the specific voice in `/v1/voices`.

**Fix** — if it is a bug in the capability declaration, fix `capabilities` in the adapter.
If it is real, the value is being dropped on purpose and you should see it in
`X-TTSGrab-Unsupported`. Set `"strict": true` to turn it into an error instead.

---

## Traditional Chinese sounds wrong

This is the honest weak spot, and it depends entirely on the engine.

| Engine | zh-TW quality | Why |
|---|---|---|
| Edge `zh-TW-*` | good | trained on Taiwanese Mandarin |
| Azure `zh-TW-*` | best | largest zh-TW voice set, SSML styles |
| Google `cmn-TW-*` | good | but Chirp 3 HD has no zh-TW |
| Kokoro `zf_*`/`zm_*` | weak | trained on Mandarin, its own authors grade it "D" |
| CosyVoice2 | good, mainland accent | zh-CN training data |
| GPT-SoVITS clone | depends on the reference | zero-shot copies the accent |

The gateway normalises Traditional → Simplified with OpenCC and applies a pronunciation
dictionary before synthesis, which fixes common Taiwan readings:

```
垃圾 → 拉基      企業 → 起業      法國 → 法郭      星期 → 星七
```

Extend it in `config.yaml`:

```yaml
longform:
  pron_overrides:
    網購: 往購
    即時: 及食
```

Confirm normalisation is happening:

```bash
curl -s -X POST localhost:8000/v1/tts/estimate -H 'Content-Type: application/json' \
  -d '{"text":"這是繁體中文","voice":"zf_xiaobei"}' | python3 -m json.tool
```

For production zh-TW: use Azure or Edge. Do not use Kokoro for Mandarin unless you have
listened to it and accepted the result.

---

## Database is locked (SQLite)

**Symptom** — `sqlite3.OperationalError: database is locked` under concurrent load.

**Cause** — SQLite allows one writer. Long-text synthesis writing history while a worker
monitor writes health rows can collide.

**Fixes**, in order:

1. It should already be in WAL mode; confirm with
   `sqlite3 data/ttsgrab.db 'PRAGMA journal_mode;'` → `wal`.
2. Reduce contention: raise `workers.health_interval_s` to 60.
3. Move to PostgreSQL. If you are running more than one gateway replica, you must — SQLite on
   a shared volume corrupts.

---

## "Task group is not initialized" on `/mcp`

Fixed in `main.py` — Starlette does not run the lifespan of mounted sub-applications, and the
MCP transport keeps its session task group there. If you see it, you are running a build
where `_McpPathNormalizer` / the explicit `mcp_inner` lifespan entry is missing.

Also note: some clients need the trailing slash (`/mcp/`). The gateway rewrites `POST /mcp`
to `/mcp/`, but a client that resolves the path itself may still 405.

---

## The UI is blank after an upgrade

**Cause** — the SPA is built into the Python package, so a stale build ships stale JS.

```bash
cd web && rm -rf dist node_modules/.vite && npm run build && cd ..
# then restart the gateway, or rebuild the image
```

**Confirm the server is serving what you think:**

```bash
curl -s localhost:8000/ | grep -o 'assets/[^"]*'
ls src/ttsgrab/web/assets/
```

The hashes must match. If `index.html` references a file that is not in `assets/`, the build
was interrupted — rebuild.

---

## Getting a real diagnosis

```bash
# what the gateway thinks is true
curl -s localhost:8000/readyz | python3 -m json.tool
curl -s localhost:8000/v1/providers | python3 -m json.tool

# structured logs, filtered to one request
docker compose logs gateway | grep '<request-id-from-the-error-body>'

# ask for the response headers, not just the body
curl -sD - -o /dev/null -X POST localhost:8000/v1/tts -H 'Content-Type: application/json' -d '…'

# reproduce without the UI or the proxy in the way
./scripts/smoke.sh http://127.0.0.1:8000
```

Every error body carries a `request_id`. Quote it — it is the fastest path from a symptom to
the log line that explains it.
