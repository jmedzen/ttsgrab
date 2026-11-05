# ttsgrab

[![ci](https://github.com/jmedzen/ttsgrab/actions/workflows/ci.yml/badge.svg)](https://github.com/jmedzen/ttsgrab/actions/workflows/ci.yml)

**One TTS API in front of many engines.** A web UI for listening and tuning, a stable REST
API for agents, and a Docker command that gets you audio in about five minutes.

Edge TTS, Google Cloud TTS, Gemini TTS, Azure Speech, and self-hosted GPU workers behind a
single `POST /v1/tts` — with routing, fallback, caching, cost accounting and a hard budget
cap you can actually trust.

```
                    ┌──────────────────────────────────────────────┐
  browser ──────────┤  Web UI (React)   /playground /compare …     │
  agent   ──────────┤  REST    POST /v1/tts  /v1/voices  /v1/usage │
  OpenAI SDK ───────┤  compat  POST /v1/audio/speech               │
  MCP client ───────┤  MCP     /mcp  synthesize_speech …           │
                    └───────────────────────┬──────────────────────┘
                                            │  one pipeline:
                            routing · fallback · cache · budget · history
                                            │
        ┌───────────────┬───────────────────┼───────────────────┬──────────────┐
        │               │                   │                   │              │
   edge (free)   google_cloud (paid)    gemini (pool)      azure (F0)   LAN workers
   unofficial    4M free/mo             no free tier       500k free    kokoro / cosyvoice
                                                                        / gpt_sovits
```

---

## Quickstart

```bash
git clone https://github.com/jmedzen/ttsgrab && cd ttsgrab
make up          # or: docker compose --profile core up -d
```

Open <http://localhost:8000>, type something, press **Synthesise**. That is Edge TTS, with
no account and no key.

Add cloud providers whenever you want them — the gateway boots fine without any:

```bash
cp .env.example .env
$EDITOR .env         # GOOGLE_API_KEY=… / AZURE_SPEECH_KEY=… / GEMINI_API_KEYS=…
docker compose --profile core up -d
```

Nothing else is required. A missing key is not an error: the provider shows as
*not enabled* with the reason, and requests route around it.

### Without Docker

```bash
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e ".[dev,mcp]"
cd web && npm install && npm run build && cd ..
python -m ttsgrab --port 8000
```

Needs `ffmpeg` on `PATH` for format conversion and long-text concatenation. Without it,
synthesis still works in the provider's native format.

---

## What you get

### One call

```bash
curl -X POST http://localhost:8000/v1/tts \
  -H 'Content-Type: application/json' \
  -d '{"text":"你好，世界","voice":"zh-TW-HsiaoChenNeural"}' \
  --output hello.mp3
```

Response headers tell you what actually happened — which provider served it, whether it
was cached, what it cost, and which parameters were dropped:

```
x-ttsgrab-provider: edge
x-ttsgrab-format: mp3
x-ttsgrab-cached: false
x-ttsgrab-cost-microusd: 0
x-ttsgrab-latency-ms: 1306
x-ttsgrab-unsupported: volume
x-ttsgrab-fallback: stub->edge
```

### Routing that fails over

```json
{"text": "…", "voice": "zh-TW-HsiaoChenNeural", "policy": "cheapest"}
```

`cheapest` · `fastest` · `best_quality` · `local_only` · `free_only` · `explicit`.
Failures fall back along the configured chain, each provider has a circuit breaker, and the
response records every attempt. Pull the plug on a GPU box and requests keep working — the
`x-ttsgrab-fallback` header names the substitution instead of hiding it.

### Parameters that are never silently ignored

Ask for `wav` from a provider that only emits mp3 and you get mp3 **plus** a `transcoded`
note and an `unsupported: ["format"]` entry. Add `"strict": true` and it becomes a hard
`422` with the exact list. Silent degradation is a bug, so the API makes it impossible to
miss.

### A budget that refuses rather than overspends

```json
{"text": "…", "voice": "en-US-Neural2-C", "provider": "google_cloud"}
```

```
HTTP 402  {"error": {"code": "budget_exceeded", "retryable": false,
                     "message": "monthly budget of $5.00 reached ($5.03 used)"}}
```

Costs are tracked in integer micro-USD against a versioned `pricing.yaml`, per
provider/tier, with monthly free allowances and requests that straddle the free/billable
boundary priced correctly. On breach the gateway stops — it does not quietly downgrade to a
free provider, because that turns a loud failure into a silent quality regression.

### The web UI

| Page | What it is for |
|---|---|
| **Playground** | All parameters, live duration/cost estimate, waveform player |
| **Compare** | Same text on up to 8 voices; first-byte latency, total time, RTF, bytes, cost |
| **Voices** | The unified catalogue, filterable, with one-click previews |
| **Providers** | Health per provider, plus LAN worker registration and health checks |
| **History** | Everything you have synthesised, replayable, deletable |
| **Usage** | Characters, cost, free-tier headroom, daily series |
| **API Keys** | Per-key provider allowlists, rate limits, monthly quotas |

Dark mode, 繁體中文 / English, responsive.

### Agents

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="ttsg_…")
with client.audio.speech.with_streaming_response.create(
    model="tts-1", voice="alloy", input="Hello from an unchanged SDK",
) as r:
    r.stream_to_file("out.mp3")
```

Only `base_url` changes. MCP clients get `synthesize_speech`, `list_voices`,
`list_providers`, `get_usage` at `/mcp`. Machines read `/llms.txt` and `/agents.md`; humans
read `/docs` (OpenAPI) or `/redoc`.

---

## Provider support

Verified against each vendor's own docs at the time of writing. **Read the licence and
stability columns before choosing.**

| Provider | Official | Cost | Free allowance | zh-TW | Stability | Default |
|---|---|---|---|---|---|---|
| **Edge TTS** | ✗ unofficial | free | unlimited | ✓ 3 voices | **fragile** — DRM-gated, has broken 6× since 2024 | **on** |
| **Google Cloud TTS** | ✓ | $4–$160 / 1M chars | 4M Standard+WaveNet, 1M Neural2/Chirp3-HD | ✓ `cmn-TW-*`, Neural2 | stable | off |
| **Gemini TTS** | ✓ | ~$12 / 1M chars | none | ✓ | preview — model id may change | off |
| **Azure Speech** | ✓ | $16 / 1M chars | 500k (F0), **fails closed** | ✓ best zh-TW coverage | stable | off |
| **Kokoro** | self-hosted | free | — | ✗ zh-CN only, weak | stable (Apache-2.0) | off |
| **CosyVoice2** | self-hosted | free | — | ✓ strong, cloneable | stable (Apache-2.0) | off |
| **GPT-SoVITS** | self-hosted | free | — | ✓ strong, cloneable | stable (MIT) | off |
| gTTS / translate_tts | ✗ unofficial | free | unlimited | ✓ | fragile, ToS-ambiguous | off |

### On the unofficial providers

Edge TTS is enabled by default because it needs no key and it is the only way to hear
something in the first 30 seconds. Be clear-eyed about it:

- Microsoft publishes **no** supported API for it. The endpoint is the Edge browser's
  read-aloud service, gated by a `Sec-MS-GEC` token that Microsoft rotates.
- It has broken for days at a time: 2024-10, 2024-11, 2025-08, 2025-12, 2026-01, 2026-10.
- Microsoft's service terms do not grant redistribution rights for the audio.
- This build retries with backoff because empty responses are routine, not exceptional.

**Do not put it behind anything you would have to apologise for.** For production, use
Google or Azure. The other unofficial adapters (gTTS, translate_tts, StreamElements) are
**off by default and show a warning in the UI** — they scrape a service that never offered
an API. StreamElements is dead outright (401 without a key it never issues).

### On self-hosted model licences

Code licence ≠ weights licence, and the weights are what you redistribute. That is why only
three workers ship:

| Model | Code | Weights | Verdict |
|---|---|---|---|
| Kokoro | Apache-2.0 | Apache-2.0 | ✅ ship |
| CosyVoice2 | Apache-2.0 | Apache-2.0 | ✅ ship |
| GPT-SoVITS | MIT | MIT | ✅ ship |
| XTTS-v2 | CPML | CPML | ❌ non-commercial |
| F5-TTS | MIT | CC-BY-NC | ❌ NC weights |
| Fish Speech | Apache-2.0-ish | CC-BY-NC-SA | ❌ NC + hosted-service clause |
| Piper | GPL-3.0 fork | mixed per voice | ❌ licence mix, no zh-TW |

The excluded ones are documented in [docs/adding-a-provider.md](docs/adding-a-provider.md)
rather than shipped. Personal non-commercial use is your call; the base class makes it a
short addition.

---

## Architecture

```
src/ttsgrab/
  api/          HTTP surface: v1/, schemas, deps (auth, rate limit)
  core/         the pipeline — router, synthesis, cache, pricing, usage,
                textsplit, audio (ffmpeg), security, obs (logs + metrics)
  providers/    in-process adapters: edge, gtts, google_cloud, gemini, azure
  workers/      client + monitor for remote HTTP model workers
  db/           SQLAlchemy models and session
  web/          the built SPA (served by FastAPI, one container)

workers/
  _base/        the three-endpoint worker protocol
  kokoro/  cosyvoice/  gpt_sovits/
```

Three replaceable layers, so adding any one does not touch the others:

1. **Provider** — a Python class in `providers/` with `@register`. In-process, no network
   hop. Adding one is one file.
2. **Worker** — any container answering `/health`, `/voices`, `/synthesize`. Registered by
   URL at runtime, health-checked, evicted on failure.
3. **Caller** — REST, OpenAI-compatible, or MCP. All three go through the same pipeline, so
   caching, budgeting and history behave identically.

Every layer reports *what happened* — the provider used, the parameters dropped, the cost
incurred — rather than pretending the abstraction is seamless. That is the whole point: an
abstraction over TTS is only useful if it tells you which engine you actually got.

---

## Configuration

`config.yaml` for structure, environment variables for secrets, env wins over the file. See
[`.env.example`](.env.example) for every knob.

```yaml
router:
  default_policy: cheapest
  fallback_chain: [edge, kokoro, google_cloud, azure]
cache:
  dir: ./data/cache
  max_bytes: 2147483648
  ttl_seconds: 2592000
security:
  max_text_chars: 8000
```

Keys are read from env, `.env`, or `*_FILE` indirection for Docker secrets
(`TTSGRAB_GOOGLE_CLOUD__CREDENTIALS_FILE`). They are never written to the database, never
logged, and never sent to the browser — all upstream traffic goes through the backend.

---

## Deployment

| Shape | How |
|---|---|
| Single box | `make up` |
| With a GPU | `make up-gpu` (needs NVIDIA runtime) |
| Laptop, no GPU | `make up-cpu` |
| Gateway + GPU in another room | `docker compose -f docker-compose.distributed.yml up -d` on the GPU box, then register its URL in **Providers** |
| Behind HTTPS | `docker compose --profile core --profile tls up`, edit `deploy/caddy/Caddyfile` |

Images build for `linux/amd64` and `linux/arm64`. On arm64 every dependency has a wheel;
the GPU workers need CUDA and therefore amd64. See
[docs/deployment.md](docs/deployment.md).

The gateway binds to `127.0.0.1` by default. It is not hardened for the open internet:
put Caddy or Traefik in front, and set `TTSGRAB_ADMIN_TOKEN` before exposing it to a LAN
you do not control.

---

## Verifying it works

```bash
make test                                  # pytest: pricing, cache, routing, adapters, security
make test-e2e                              # Playwright: real synthesis in the browser
./scripts/smoke.sh http://127.0.0.1:8000   # the acceptance checklist, live
```

`scripts/smoke.sh` walks the brief's acceptance criteria and prints one line per check.
Checks that need credentials it does not have are reported as **skipped**, not as passes.

Honest status of the acceptance list:

| # | Criterion | Status |
|---|---|---|
| 1 | `docker compose --profile core up` → Edge TTS audible in UI | ✅ the gateway **image** is built and smoke-tested in CI: container starts, 322 voices, 16 992 bytes of real audio from Edge, UI served. The full `core` profile with worker containers is still unbuilt here (no Docker on this machine) |
| 2 | Google credentials → voices appear, usage correct | ⚠️ adapter unit-tested against a mocked API; **not** verified against the live Google API (no account) |
| 3 | Register LAN worker, then kill it → auto-fallback | ✅ verified end-to-end (`x-ttsgrab-fallback: stub->edge`) |
| 4 | OpenAI SDK with only `base_url` changed | ✅ verified |
| 5 | MCP client lists voices and synthesises | ✅ verified with the real MCP client |
| 6 | Compare page across ≥3 providers | ✅ verified e2e |
| 7 | No keys in logs, frontend or git history | ✅ log scrubber + secret-pattern smoke check; `gitleaks` in CI |

What CI proves on every push: lint, mypy, 127 tests on Python 3.11 and 3.12, the SPA
typecheck and build, six Playwright tests against a live server, the 13-check smoke script,
a `linux/amd64` gateway image build, a container boot that synthesises real audio, the CPU
worker image, and a full-history secret scan.

The remaining gaps are honest ones. Google's live API needs an account this repo does not
have. Docker is not installed on the machine the project was written on, so the multi-container
`core` / `gpu` profiles — gateway plus model workers — have never been started together,
and no GPU worker has ever loaded a model. Those paths are unit-tested against fakes, not
against reality.

---

## Docs

- [Adding a provider](docs/adding-a-provider.md) — the one-file tutorial, plus the models
  that were deliberately left out and why
- [API reference](docs/api.md) — every endpoint, every error code
- [Deployment](docs/deployment.md) — single box, distributed GPU, HTTPS, Postgres
- [Troubleshooting](docs/troubleshooting.md) — the failures you will actually hit
- [`/llms.txt`](http://localhost:8000/llms.txt) and [`AGENTS.md`](AGENTS.md) — for agents

## Licence

MIT for this gateway. **The providers, models and voices have their own licences** — see
the tables above before you build a product on top of any of them.
