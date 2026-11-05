"""Core TTS endpoints: sync synthesis, streaming, compare."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections import deque
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from ttsgrab.api.deps import Ctx, PrincipalDep, check_rate_limit
from ttsgrab.api.schemas import CompareRequest, TTSRequest, TTSResponse
from ttsgrab.core.errors import Forbidden, InvalidRequest
from ttsgrab.core.obs import SYNTH_CHARS, SYNTH_COST, SYNTH_LATENCY, SYNTH_REQUESTS
from ttsgrab.core.synthesis import synthesize
from ttsgrab.core.types import SynthRequest

router = APIRouter(tags=["tts"])


def _to_synth_request(body: TTSRequest) -> SynthRequest:
    try:
        voice = body.resolved_voice()
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    return SynthRequest(
        text=body.text,
        voice_id=voice,
        provider=body.provider,
        format=body.resolved_format(),
        sample_rate=body.sample_rate,
        speed=body.speed,
        pitch=body.pitch,
        volume=body.volume,
        ssml=body.ssml,
        style=body.style,
        prompt=body.prompt,
        language=body.language,
        reference_audio_id=body.reference_audio_id,
        strict=body.strict,
        extra=body.extra,
    )


def _guard_provider(principal, provider: str | None) -> None:
    if provider and principal.provider_allowlist and not principal.allows_provider(provider):
        raise Forbidden(
            f"this API key may not use provider {provider!r}",
            details={"allowlist": principal.provider_allowlist},
        )


@router.post("/v1/tts", summary="Synthesise speech (synchronous)")
async def tts(
    body: TTSRequest,
    request: Request,
    ctx: Ctx,
    principal: PrincipalDep,
) -> Response:
    await check_rate_limit(ctx, principal, "tts")
    _guard_provider(principal, body.provider)
    req = _to_synth_request(body)

    outcome = await synthesize(
        ctx,
        req,
        policy=body.policy,
        allow_fallback=body.allow_fallback,
        bypass_cache=body.bypass_cache,
        api_key_id=principal.id,
        client=principal.name,
    )
    result = outcome.result

    SYNTH_REQUESTS.labels(result.provider, "ok", str(result.from_cache)).inc()
    SYNTH_LATENCY.labels(result.provider).observe((result.latency_ms or 0) / 1000)
    SYNTH_CHARS.labels(result.provider, outcome.meta.get("tier") or "unknown").inc(
        result.billed_characters
    )
    SYNTH_COST.labels(result.provider).inc(result.cost_micro_usd)

    if body.response == "json":
        payload = TTSResponse(
            id=outcome.history_id or "",
            history_id=outcome.history_id,
            provider=result.provider,
            voice_id=result.voice_id,
            format=result.format.value,
            mime_type=result.format.mime,
            audio_base64=base64.b64encode(result.audio).decode("ascii"),
            audio_url=f"/v1/history/{outcome.history_id}/audio" if outcome.history_id else None,
            bytes=len(result.audio),
            duration_ms=outcome.duration_ms,
            latency_ms=result.latency_ms,
            first_byte_ms=result.first_byte_ms,
            characters=result.billed_characters,
            cost_micro_usd=result.cost_micro_usd,
            cost_usd=round(result.cost_micro_usd / 1_000_000, 6),
            used_free_tier=result.used_free_tier,
            cached=result.from_cache,
            unsupported=result.unsupported,
            transcoded=list(result.transcoded or []),
            fallback_used=bool(outcome.meta.get("fallback_used")),
            requested_provider=outcome.meta.get("requested_provider"),
            policy=outcome.meta.get("policy"),
            attempts=outcome.meta.get("attempts") or [],
        )
        return Response(
            content=payload.model_dump_json(),
            media_type="application/json",
            headers=_headers(outcome),
        )

    return Response(
        content=result.audio,
        media_type=result.format.mime,
        headers=_headers(outcome),
    )


def _headers(outcome) -> dict[str, str]:
    result = outcome.result
    headers = {
        "X-TTSGrab-Provider": result.provider,
        "X-TTSGrab-Voice": result.voice_id,
        "X-TTSGrab-Format": result.format.value,
        "X-TTSGrab-Cached": "true" if result.from_cache else "false",
        "X-TTSGrab-Cost-MicroUSD": str(result.cost_micro_usd),
        "X-TTSGrab-Latency-Ms": str(result.latency_ms or 0),
        "Content-Disposition": (
            f'inline; filename="ttsgrab-{outcome.history_id or "audio"}.{result.format.value}"'
        ),
    }
    if outcome.history_id:
        headers["X-TTSGrab-History-Id"] = outcome.history_id
    if result.unsupported:
        headers["X-TTSGrab-Unsupported"] = ",".join(result.unsupported)
    if outcome.meta.get("fallback_used"):
        headers["X-TTSGrab-Fallback"] = (
            f"{outcome.meta.get('requested_provider')}->{result.provider}"
        )
    return headers


@router.post(
    "/v1/tts/stream",
    summary="Synthesise speech as a stream",
    response_class=StreamingResponse,
)
async def tts_stream(
    body: TTSRequest,
    request: Request,
    ctx: Ctx,
    principal: PrincipalDep,
) -> StreamingResponse:
    """Stream audio chunks.

    Two modes:
    * ``stream_format=audio`` (default): chunked audio of the requested container.
    * ``stream_format=sse``: Server-Sent Events with base64 chunks + a final metadata
      event, for browser EventSource clients that cannot consume binary streams.

    Long text is split into sentences and pipelined: sentence *i+1* starts synthesising
    while sentence *i* is still downloading. That is what makes first-audio latency
    roughly independent of total length.
    """
    await check_rate_limit(ctx, principal, "tts")
    _guard_provider(principal, body.provider)
    stream_format = request.query_params.get("stream_format", "audio")
    req = _to_synth_request(body)

    provider = ctx.registry.get(body.provider) if body.provider else None
    if provider is None:
        owner = ctx.registry.voice_exists_anywhere(req.voice_id)
        provider = ctx.registry.get(owner) if owner else None

    async def _audio_iter():
        from ttsgrab.core.textsplit import plan_text

        plan = plan_text(
            req.text,
            normalize=ctx.settings.longform.enabled,
            dictionary=ctx.dictionary,
            chunk_threshold=0,
            max_sentence_chars=ctx.settings.longform.max_sentence_chars,
            target_language=req.language,
        )
        # Bound concurrent synthesis; the queue below decides how far ahead we run.
        sem = asyncio.Semaphore(ctx.settings.longform.max_concurrency)

        async def _synth(chunk: str):
            async with sem:
                return await synthesize(
                    ctx,
                    req.model_copy(update={"text": chunk}),
                    policy=body.policy,
                    allow_fallback=body.allow_fallback,
                    bypass_cache=body.bypass_cache,
                    api_key_id=principal.id,
                    client=principal.name,
                    persist=False,
                    pre_split=True,
                )

        try:
            # A streaming caller hears nothing until the *first* chunk is ready, so the
            # window must stay small. Running the whole document at once means the first
            # byte arrives when the last does, and the endpoint streams in name only. Two
            # keeps one chunk synthesising while the previous one is being consumed.
            window = 2
            pending: deque[asyncio.Task] = deque()
            next_chunk = 0
            emitted = 0

            def fill() -> None:
                nonlocal next_chunk
                while len(pending) < window and next_chunk < len(plan.chunks):
                    pending.append(asyncio.create_task(_synth(plan.chunks[next_chunk])))
                    next_chunk += 1

            fill()

            while pending:
                task = pending.popleft()
                outcome = await task
                # Start the next chunk only once this one has been handed over, so the
                # provider is never working on the whole document in advance.
                fill()

                if stream_format == "sse":
                    yield _sse(
                        "chunk",
                        {
                            "index": emitted,
                            "audio_base64": base64.b64encode(outcome.result.audio).decode(),
                            "format": outcome.result.format.value,
                            "provider": outcome.result.provider,
                        },
                    )
                elif pending or next_chunk < len(plan.chunks):
                    yield outcome.result.audio
                else:
                    # Last chunk, and the only one: let the provider stream it byte by byte
                    # so a single-sentence request still gets a low first-byte latency.
                    if emitted == 0 and provider is not None and provider.capabilities.streaming:
                        async for blob in provider.stream(
                            req.model_copy(update={"provider": provider.name})
                        ):
                            yield blob
                    else:
                        yield outcome.result.audio
                emitted += 1

            if stream_format == "sse":
                yield _sse("done", {"chunks": len(plan.chunks)})
        finally:
            # A caller that hangs up must not leave the provider synthesising for minutes.
            # Cancelling only *requests* the stop; awaiting is what makes the generator
            # close before this response is torn down.
            cancelled = [t for t in pending if not t.done()]
            for task in cancelled:
                task.cancel()
            for task in cancelled:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    media = "text/event-stream" if stream_format == "sse" else req.format.mime
    return StreamingResponse(
        _audio_iter(),
        media_type=media,
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "X-TTSGrab-Format": req.format.value,
        },
    )


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


@router.post("/v1/compare", summary="Synthesise the same text across several providers")
async def compare(
    body: CompareRequest,
    ctx: Ctx,
    principal: PrincipalDep,
) -> dict[str, Any]:
    """A/B comparison. Runs every target concurrently and returns metrics + audio URLs."""
    await check_rate_limit(ctx, principal, "compare")
    if not body.targets:
        raise InvalidRequest("`targets` must contain at least one {provider, voice_id}")
    if len(body.targets) > 8:
        raise InvalidRequest("at most 8 targets per comparison")

    async def _one(target: dict[str, str]) -> dict[str, Any]:
        provider = target.get("provider")
        voice_id = target.get("voice_id") or target.get("voice")
        if not provider or not voice_id:
            return {"provider": provider, "voice_id": voice_id, "error": "missing field"}
        _guard_provider(principal, provider)
        req = SynthRequest(
            text=body.text,
            voice_id=voice_id,
            provider=provider,
            format=body.format,
            speed=body.speed,
            pitch=body.pitch,
        )
        import time

        started = time.perf_counter()
        try:
            outcome = await synthesize(
                ctx,
                req,
                policy="explicit",
                allow_fallback=False,
                api_key_id=principal.id,
                client=principal.name,
            )
        except Exception as exc:
            return {
                "provider": provider,
                "voice_id": voice_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            }
        wall_ms = int((time.perf_counter() - started) * 1000)
        result = outcome.result
        rtf = (
            round((result.latency_ms or wall_ms) / outcome.duration_ms, 3)
            if outcome.duration_ms
            else None
        )
        return {
            "provider": result.provider,
            "voice_id": result.voice_id,
            "history_id": outcome.history_id,
            "audio_url": f"/v1/history/{outcome.history_id}/audio" if outcome.history_id else None,
            "format": result.format.value,
            "bytes": len(result.audio),
            "duration_ms": outcome.duration_ms,
            "latency_ms": result.latency_ms,
            "first_byte_ms": result.first_byte_ms,
            "rtf": rtf,
            "characters": result.billed_characters,
            "cost_micro_usd": result.cost_micro_usd,
            "cost_usd": round(result.cost_micro_usd / 1_000_000, 6),
            "cached": result.from_cache,
            "unsupported": result.unsupported,
        }

    results = await asyncio.gather(*(_one(t) for t in body.targets))
    return {"text_chars": len(body.text), "results": results}


@router.post("/v1/tts/estimate", summary="Estimate duration and cost without synthesising")
async def estimate(body: TTSRequest, ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    from ttsgrab.core.textsplit import estimate_speech_seconds
    from ttsgrab.db.session import session_scope

    req = _to_synth_request(body)
    provider_name = body.provider or ctx.registry.voice_exists_anywhere(req.voice_id)
    tier = None
    cost = 0
    free = True
    if provider_name:
        provider = ctx.registry.get(provider_name)
        if provider is not None:
            tier = next(
                (v.tier for v in provider.cached_voices if v.voice_id == req.voice_id), None
            )
            async with session_scope() as session:
                cost, free = await ctx.usage.estimate(
                    session, provider=provider_name, tier=tier, characters=len(body.text)
                )
    return {
        "characters": len(body.text),
        "estimated_duration_ms": int(estimate_speech_seconds(body.text, body.speed) * 1000),
        "provider": provider_name,
        "tier": tier,
        "estimated_cost_micro_usd": cost,
        "estimated_cost_usd": round(cost / 1_000_000, 6),
        "within_free_tier": free,
    }
