"""Agent-facing documentation: ``/llms.txt`` and ``/agents.md``.

Agents read these before making calls, so they are generated from the live configuration
rather than hand-written: the voice list, provider list and limits stay accurate as soon
as config changes.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from ttsgrab.api.deps import Ctx

router = APIRouter(tags=["docs"])


def _llms_txt(ctx) -> str:
    providers = [p for p in ctx.registry.all() if p.enabled]
    names = ", ".join(p.name for p in providers) or "(none configured)"
    base = ctx.settings.public_base_url
    return f"""# ttsgrab

> A unified text-to-speech gateway. One API over {len(providers)} providers.

Base URL: {base}
Auth: `Authorization: Bearer <key>` (or `X-API-Key`). Local unauthenticated access is
allowed only when no key is configured and the request comes from localhost.

## The one call you probably want

POST {base}/v1/tts
Content-Type: application/json

{{"text": "你好，世界", "voice": "zh-TW-HsiaoChenNeural"}}

Returns raw audio bytes (mp3 by default). Read `X-TTSGrab-Provider` to learn which
provider actually served it, and `X-TTSGrab-Cached` to know whether it was cached.

## Do this instead of guessing a voice

GET {base}/v1/voices?language=zh-TW&free_only=true
Pick a `voice_id` from the response. Never invent a voice id.

## Choice of provider

Pass `"provider": "<name>"` to force one, or `"policy"` to let the gateway choose:
  cheapest | fastest | best_quality | local_only | free_only | explicit
Available providers right now: {names}

Failures fall back automatically (`allow_fallback: true` by default) and the response
says which provider was used. Set `"allow_fallback": false` when you need strictness.

## Other endpoints

POST {base}/v1/tts/stream          chunked audio (or `?stream_format=sse` for EventSource)
POST {base}/v1/compare             same text, several voices, with metrics
POST {base}/v1/jobs                async job, optional `webhook_url`
GET  {base}/v1/jobs/{{id}}           poll it
POST {base}/v1/audio/speech        OpenAI-compatible (change only `base_url` in the SDK)
GET  {base}/v1/voices              the catalogue
GET  {base}/v1/providers           health and status
GET  {base}/v1/usage               characters and cost this month
GET  {base}/v1/history             past syntheses
POST {base}/v1/tts/estimate        duration and cost without synthesising

## Errors

Every error is {{"error": {{"code", "message", "retryable", "suggested_fallback"}}}}.
Branch on `error.code`, not on the message text. `retryable: false` means do not retry —
a retry cannot succeed (bad params, quota, budget). `suggested_fallback` lists providers
that could work instead.

## Limits

Max text: {ctx.settings.security.max_text_chars} characters per request.
Longer text is split into sentences automatically and synthesised in parallel, so the
request can exceed the limit only if `max_text_chars` is raised.

## MCP

The same capabilities are exposed over MCP at {base}/mcp with tools
`synthesize_speech`, `list_voices`, `list_providers`.
"""


def _agents_md(ctx) -> str:
    base = ctx.settings.public_base_url
    return f"""# AGENTS.md — using ttsgrab

Concise operating instructions for an autonomous agent. Humans should read README.md.

## TL;DR

```bash
curl -X POST {base}/v1/tts \\
  -H "Authorization: Bearer $TTSGRAB_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{{"text":"你好，世界","voice":"zh-TW-HsiaoChenNeural"}}' \\
  --output out.mp3
```

## Decision procedure

1. **Discover voices** (`GET /v1/voices`). Filter by `language` and, if cost matters,
   `free_only=true`. Cache the result in your own context — it changes rarely.
2. **Decide cost tolerance.** If the task is user-facing and quality matters, use
   `"policy": "best_quality"`. If it is bulk or background work, use `"policy": "cheapest"`
   or `"free_only": true`.
3. **Synthesise** with `POST /v1/tts`. Prefer `response: "json"` if you need metadata in
   the same round trip; use the default binary response if you just want bytes.
4. **Check the response headers.** `X-TTSGrab-Unsupported` lists parameters the provider
   ignored. If a parameter you need was dropped, re-request with `"strict": true` to get a
   hard error instead of silently degraded audio.

## Rules

- Never invent a `voice_id`. Query `/v1/voices` first.
- Listen to `error.retryable`. Retrying a `budget_exceeded` or `unsupported_param` wastes
  time and money.
- On `all_providers_failed`, try `error.suggested_fallback`. If that also fails, surface
  the failure — do not silently produce silence.
- Do not send personally identifying content to unofficial providers (`edge`, `gtts`,
  `translate_tts`). `GET /v1/providers` marks them with `"official": false`.
- For text over ~2000 characters, prefer `POST /v1/jobs` and poll, so you do not sit on a
  long HTTP connection.

## Locale note (Traditional Chinese)

Local models are trained on Mandarin (zh-CN) lexicons. The gateway normalises Traditional
→ Simplified and applies a pronunciation dictionary before synthesis, but Taiwan-specific
readings can still differ. For production zh-TW, prefer `edge` (`zh-TW-*Neural`) or a
Google/Azure zh-TW voice.

## MCP

If you are an MCP client, mount `{base}/mcp` and use:
- `synthesize_speech({{text, voice, format?, speed?, provider?, policy?}})`
- `list_voices({{language?, provider?, free_only?}})`
- `list_providers({{}})`
"""


@router.get("/llms.txt", response_class=PlainTextResponse, include_in_schema=False)
async def llms_txt(ctx: Ctx) -> str:
    return _llms_txt(ctx)


@router.get("/agents.md", response_class=PlainTextResponse, include_in_schema=False)
async def agents_md(ctx: Ctx) -> str:
    return _agents_md(ctx)
