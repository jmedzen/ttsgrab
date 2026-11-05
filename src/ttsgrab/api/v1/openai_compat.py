"""OpenAI-compatible ``POST /v1/audio/speech``.

The point: an existing OpenAI SDK or tool should work by only changing ``base_url``.
That means matching the request body, the response content type, and the error shape as
closely as we can while keeping our richer behaviour available.

Mapping decisions
-----------------
* ``voice`` may be an OpenAI alias (alloy/nova/…) *or* a real ttsgrab voice id. Aliases
  are mapped per language, because "alloy" means nothing to a zh-TW engine.
* ``model`` is accepted and, if it matches a known voice, used as a voice id — some
  clients put the voice there.
* ``response_format`` maps directly; ``speed`` maps directly.
* Errors are returned as OpenAI's ``{"error": {"message", "type", "code"}}`` shape so
  SDKs that parse it do not blow up.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from ttsgrab.api.deps import Ctx, PrincipalDep, check_rate_limit
from ttsgrab.core.errors import GatewayError
from ttsgrab.core.synthesis import synthesize
from ttsgrab.core.types import AudioFormat, SynthRequest

router = APIRouter(tags=["openai"])

# OpenAI's built-in voice names → our defaults. Deliberately opinionated: pick a voice
# that is genuinely good in the gateway's default languages rather than a random match.
ALIASES: dict[str, dict[str, str]] = {
    "alloy": {"edge": "en-US-AriaNeural", "default": "zh-TW-HsiaoChenNeural"},
    "echo": {"edge": "en-US-GuyNeural", "default": "zh-TW-YunJheNeural"},
    "fable": {"edge": "en-GB-SoniaNeural", "default": "zh-TW-HsiaoYuNeural"},
    "onyx": {"edge": "en-US-AndrewMultilingualNeural", "default": "zh-TW-YunJheNeural"},
    "nova": {"edge": "en-US-AvaMultilingualNeural", "default": "zh-TW-HsiaoChenNeural"},
    "shimmer": {"edge": "en-US-EmmaMultilingualNeural", "default": "zh-TW-HsiaoYuNeural"},
}

_FORMAT_MAP = {
    "mp3": AudioFormat.MP3,
    "opus": AudioFormat.OPUS,
    "aac": AudioFormat.MP3,  # we have no AAC encoder; mp3 is the closest we can serve
    "flac": AudioFormat.FLAC,
    "wav": AudioFormat.WAV,
    "pcm": AudioFormat.WAV,
}


class OpenAISpeechRequest(BaseModel):
    model: str = "tts-1"
    input: str = Field(..., min_length=1)
    voice: str = "alloy"
    response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    instructions: str | None = Field(
        default=None, description="OpenAI's style instructions; mapped to our prompt field."
    )
    # ttsgrab extensions, ignored by OpenAI clients.
    language: str | None = None
    provider: str | None = None

    model_config = {"extra": "allow"}


def _resolve_voice(ctx, voice: str, language: str | None, provider: str | None) -> str:
    # 1. A real voice id always wins.
    if ctx.registry.voice_exists_anywhere(voice):
        return voice
    # 2. Alias mapping.
    if voice.lower() in ALIASES:
        table = ALIASES[voice.lower()]
        wanted = "zh-TW" if (language or "").lower().startswith("zh") else "en-US"
        prefixed = table.get(provider or "", table["default"])
        # Prefer a voice in the requested language if the alias default is English.
        for candidate in (prefixed, table.get("edge", ""), table["default"]):
            if not candidate:
                continue
            for v in ctx.registry.catalogue():
                if v.voice_id == candidate and any(
                    lg.lower().startswith(wanted.split("-")[0]) for lg in v.languages
                ):
                    return candidate
        return prefixed or table["default"]
    # 3. Unknown: pass it through; the provider will produce a clear error.
    return voice


def _openai_error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "param": None, "code": code}},
    )


@router.post("/v1/audio/speech", summary="OpenAI-compatible speech synthesis")
async def openai_speech(
    body: OpenAISpeechRequest,
    request: Request,
    ctx: Ctx,
    principal: PrincipalDep,
) -> Response:
    await check_rate_limit(ctx, principal, "openai")
    voice = _resolve_voice(ctx, body.voice, body.language, body.provider)
    fmt = _FORMAT_MAP.get(body.response_format, AudioFormat.MP3)

    req = SynthRequest(
        text=body.input,
        voice_id=voice,
        provider=body.provider,
        format=fmt,
        speed=body.speed,
        prompt=body.instructions,
        language=body.language,
    )
    try:
        outcome = await synthesize(
            ctx, req, api_key_id=principal.id, client=f"openai-sdk:{principal.name}"
        )
    except GatewayError as exc:
        # Translate to OpenAI's error envelope so SDKs surface something sensible.
        return _openai_error(exc.http_status, exc.code.value, exc.message)

    result = outcome.result
    headers = {
        "X-TTSGrab-Provider": result.provider,
        "X-TTSGrab-Voice": result.voice_id,
        "X-TTSGrab-Format": result.format.value,
        "X-TTSGrab-Cached": "true" if result.from_cache else "false",
        "X-TTSGrab-Cost-MicroUSD": str(result.cost_micro_usd),
    }
    if result.format is not fmt and body.response_format != "pcm":
        # We could not honour the container exactly; say so in a header rather than
        # silently returning a different format than the SDK asked for.
        headers["X-TTSGrab-Format-Substituted"] = f"{body.response_format}->{result.format.value}"

    return Response(content=result.audio, media_type=result.format.mime, headers=headers)


@router.get("/v1/models", summary="OpenAI-compatible model listing")
async def openai_models(ctx: Ctx) -> dict[str, Any]:
    """Some SDKs call this on startup. It costs nothing to be polite."""
    return {
        "object": "list",
        "data": [
            {"id": "tts-1", "object": "model", "created": 0, "owned_by": "ttsgrab"},
            {"id": "tts-1-hd", "object": "model", "created": 0, "owned_by": "ttsgrab"},
        ],
    }
