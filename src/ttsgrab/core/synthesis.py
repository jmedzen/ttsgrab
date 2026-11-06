"""The single synthesis pipeline used by every entry point.

``POST /v1/tts``, ``/v1/audio/speech``, jobs, MCP and the UI all funnel through
:func:`synthesize`. That is what keeps caching, usage metering, history, budgets and
fallback behaviour consistent no matter how a caller arrives.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any

from ttsgrab.core.audio import concat, probe_duration_ms, transcode
from ttsgrab.core.context import AppContext
from ttsgrab.core.errors import InvalidRequest, TextTooLong, UnsupportedParam
from ttsgrab.core.router import Policy
from ttsgrab.core.textsplit import estimate_speech_seconds, plan_text
from ttsgrab.core.types import SpeechSegment, SynthRequest, SynthResult
from ttsgrab.db.models import SynthRecord
from ttsgrab.db.session import session_scope


@dataclass
class SynthesisOutcome:
    result: SynthResult
    meta: dict[str, Any]
    history_id: str | None
    audio_path: str | None
    chunks: int
    normalized_text: str
    duration_ms: int | None


async def synthesize(
    ctx: AppContext,
    req: SynthRequest,
    *,
    policy: Policy | None = None,
    allow_fallback: bool = True,
    bypass_cache: bool = False,
    api_key_id: str | None = None,
    client: str | None = None,
    char_quota: int | None = None,
    persist: bool = True,
    job_id: str | None = None,
    pre_split: bool = False,
) -> SynthesisOutcome:
    """Run the full pipeline for one request.

    ``pre_split=True`` tells the pipeline the caller has already divided the text into
    sentences it will synthesise sequentially. Without it, a streaming caller that sends
    sentence one would have it re-split and synthesised in parallel *again* inside this
    function — the outer pipeline would then be waiting on the whole document, and
    first-byte latency would equal total latency.
    """
    settings = ctx.settings

    text = (req.text or "").strip()
    if not text:
        raise InvalidRequest("`text` must not be empty")
    if len(text) > settings.security.max_text_chars:
        raise TextTooLong(
            f"text is {len(text)} chars; limit is {settings.security.max_text_chars}",
            details={"length": len(text), "limit": settings.security.max_text_chars},
        )

    # Resolve provider from the voice id when the caller gave only a voice.
    if not req.provider:
        owner = ctx.registry.voice_exists_anywhere(req.voice_id)
        if owner:
            req = req.model_copy(update={"provider": owner})

    # Per-key monthly quota. This was implemented, stored, surfaced by the API and
    # documented — and never called, so a key with a 1000-character allowance happily
    # consumed 3700. Charged before any provider spend, like the budget check.
    if api_key_id and char_quota:
        async with session_scope() as session:
            await ctx.usage.check_key_quota(
                session, api_key_id=api_key_id, characters=len(text), quota=char_quota
            )

    provider = ctx.registry.get(req.provider or "")
    if provider is not None:
        tier = next((v.tier for v in provider.cached_voices if v.voice_id == req.voice_id), None)
        # Budget check happens before any spend, on the tier we are about to use.
        async with session_scope() as session:
            await ctx.usage.check_budget(
                session,
                provider=provider.name,
                tier=tier,
                characters=len(text),
            )

    # --- long-form plan ---------------------------------------------------------------
    language = req.language
    if language is None and provider is not None:
        language = next(
            (
                v.languages[0]
                for v in provider.cached_voices
                if v.voice_id == req.voice_id and v.languages
            ),
            None,
        )

    plan = plan_text(
        text,
        normalize=settings.longform.enabled,
        dictionary=ctx.dictionary,
        # A caller that already split the text wants one chunk out of this call.
        # A subtitle request needs per-sentence timing, and short text would otherwise be
        # sent as one chunk and have none. Forcing the split makes every sentence a
        # separately measured unit at the cost of one extra request per sentence.
        chunk_threshold=0
        if (pre_split or req.want_segments)
        else settings.longform.chunk_threshold,
        max_sentence_chars=settings.longform.max_sentence_chars,
        target_language=language,
    )

    chunk_req = req.model_copy(update={"text": plan.normalized})
    if len(plan.chunks) == 1:
        # Keep the original text when nothing was split or normalised, so the cache key
        # matches what the caller sent.
        chunk_req = req.model_copy(update={"text": plan.normalized})

    cache_key = chunk_req.cache_key()
    if ctx.cache.enabled and not bypass_cache:
        entry = await ctx.cache.get(cache_key)
        if entry is not None:
            blob = await ctx.cache.read(entry)
            duration = await probe_duration_ms(blob, entry.format)
            cached_tier = entry.meta.get("tier")
            cached_segments = entry.meta.get("segments") or []
            result = SynthResult(
                audio=blob,
                format=entry.format,
                sample_rate=entry.meta.get("sample_rate"),
                provider=entry.meta.get("used_provider", entry.provider),
                voice_id=entry.voice_id,
                billed_characters=0,
                cost_micro_usd=0,
                used_free_tier=True,
                latency_ms=0,
                from_cache=True,
                segments=[SpeechSegment(**seg) for seg in cached_segments],
                extra={"cache_key": cache_key},
            )
            history_id = None
            if persist:
                history_id, _p = await _persist(
                    ctx,
                    req,
                    result,
                    {
                        "policy": "cache",
                        "used_provider": result.provider,
                        "cached": True,
                        "tier": cached_tier,
                    },
                    blob,
                    duration,
                    chunks=1,
                    normalized=plan.normalized,
                    api_key_id=api_key_id,
                    client=client,
                )
            async with session_scope() as session:
                await ctx.usage.record(
                    session,
                    provider=result.provider,
                    voice_id=result.voice_id,
                    tier=cached_tier,
                    characters=0,
                    cost_micro_usd=0,
                    free_tier=True,
                    cached=True,
                    api_key_id=api_key_id,
                    job_id=job_id,
                )
            return SynthesisOutcome(
                result=result,
                meta={"cached": True, "policy": "cache"},
                history_id=history_id,
                audio_path=None,
                chunks=1,
                normalized_text=plan.normalized,
                duration_ms=duration,
            )

    # --- per-chunk synthesis ------------------------------------------------------------
    started = time.perf_counter()
    if len(plan.chunks) == 1:
        result, meta = await ctx.orchestrator.synthesize(
            chunk_req, policy=policy, allow_fallback=allow_fallback, api_key_id=api_key_id
        )
        parts = [result.audio]
        first_byte_ms = result.first_byte_ms
    else:
        sem = asyncio.Semaphore(settings.longform.max_concurrency)
        sub_reqs = [chunk_req.model_copy(update={"text": c}) for c in plan.chunks]

        async def _one(sub: SynthRequest) -> tuple[SynthResult, dict[str, Any]]:
            async with sem:
                return await ctx.orchestrator.synthesize(
                    sub, policy=policy, allow_fallback=allow_fallback, api_key_id=api_key_id
                )

        results = await asyncio.gather(*(_one(r) for r in sub_reqs))
        parts = [r.audio for r, _m in results]
        result = results[0][0]
        meta = results[0][1]
        meta["chunks"] = len(results)
        meta["providers_used"] = sorted({r.provider for r, _ in results})
        first_byte_ms = min(
            (r.first_byte_ms for r, _ in results if r.first_byte_ms is not None), default=None
        )
        merged_unsupported: set[str] = set()
        total_cost = 0
        total_chars = 0
        for r, _m in results:
            merged_unsupported.update(r.unsupported)
            total_cost += r.cost_micro_usd
            total_chars += r.billed_characters
        result = result.model_copy(
            update={
                "unsupported": sorted(merged_unsupported),
                "cost_micro_usd": total_cost,
                "billed_characters": total_chars,
            }
        )

        merged = await concat(parts, result.format)
        # Accumulate each chunk's duration so subtitle timings come out of measurements we
        # already have, rather than a second pass over the joined audio.
        segments: list[SpeechSegment] = []
        cursor = 0
        for (r, _m), chunk_text in zip(results, plan.chunks, strict=False):
            seg_ms = r.duration_ms or await probe_duration_ms(r.audio, r.format) or 0
            segments.append(SpeechSegment(text=chunk_text, start_ms=cursor, end_ms=cursor + seg_ms))
            cursor += seg_ms
        # The cue text should be what the caller wrote, not the Simplified form the
        # provider needed. Split the original the same way and pair positionally.
        if req.want_segments and req.text != plan.normalized:
            try:
                original_plan = plan_text(
                    req.text,
                    normalize=False,
                    dictionary=None,
                    chunk_threshold=0,
                    max_sentence_chars=settings.longform.max_sentence_chars,
                    target_language=req.language,
                )
                from ttsgrab.core.subtitles import restore_original_text

                segments = restore_original_text(segments, original_plan.chunks)
            except Exception:
                pass
        result = result.model_copy(update={"audio": merged, "segments": segments})
        # Chunks share the first result's format assumptions; re-probe the join.
        result = result.model_copy(update={"duration_ms": None})

    # --- format / sample-rate post-processing -------------------------------------------
    target_format = req.format
    transcoded: list[str] = list(result.transcoded)
    if result.format is not target_format:
        result = result.model_copy(
            update={
                "audio": await transcode(
                    result.audio,
                    src_format=result.format,
                    dst_format=target_format,
                    sample_rate=req.sample_rate,
                ),
                "format": target_format,
            }
        )
        transcoded.append("format")
    elif req.sample_rate and result.sample_rate and req.sample_rate != result.sample_rate:
        result = result.model_copy(
            update={
                "audio": await transcode(
                    result.audio,
                    src_format=result.format,
                    dst_format=result.format,
                    sample_rate=req.sample_rate,
                ),
                "sample_rate": req.sample_rate,
            }
        )
        transcoded.append("sample_rate")
    # --- loudness normalisation ---------------------------------------------------------
    # Runs after format/sample-rate conversion so it only ever encodes once per change.
    if req.loudness is not None:
        from ttsgrab.core.audio import normalize_loudness, resolve_loudness_target

        target = resolve_loudness_target(req.loudness)
        if target is not None:
            result = result.model_copy(
                update={
                    "audio": await normalize_loudness(
                        result.audio,
                        fmt=result.format,
                        target_lufs=target,
                        sample_rate=req.sample_rate,
                    ),
                    # Duration changes by a few ms and the join was re-probed above, so
                    # re-measure rather than trusting the pre-normalisation value.
                    "duration_ms": None,
                }
            )
            transcoded.append("loudness")

    # --- provenance ---------------------------------------------------------------------
    # Tagged last, so the tags describe the audio that actually ships (after any transcode
    # or normalisation). ffmpeg copies the stream, so this never costs quality.
    if settings.write_metadata:
        from ttsgrab.core.provenance import embed_provenance

        result = result.model_copy(
            update={
                "audio": await embed_provenance(
                    result.audio,
                    fmt=result.format,
                    payload={
                        "provider": result.provider,
                        "voice": result.voice_id,
                        "tier": str(meta.get("tier") or ""),
                        "title": text[:200],
                        "chars": str(result.billed_characters or len(text)),
                    },
                )
            }
        )

    if transcoded:
        result = result.model_copy(update={"transcoded": sorted(set(transcoded))})

    if req.strict and result.unsupported:
        raise UnsupportedParam(
            f"{result.provider} could not honour: {', '.join(result.unsupported)}",
            param=", ".join(result.unsupported),
            details={"provider": result.provider, "unsupported": result.unsupported},
        )

    duration = result.duration_ms or await probe_duration_ms(result.audio, result.format)
    total_latency = int((time.perf_counter() - started) * 1000)
    result = result.model_copy(
        update={
            "duration_ms": duration,
            "latency_ms": total_latency,
            "first_byte_ms": first_byte_ms,
        }
    )

    # --- metering -------------------------------------------------------------------------
    tier = meta.get("tier")
    if provider is not None:
        actual = ctx.registry.get(result.provider)
        if actual is not None:
            tier = next(
                (v.tier for v in actual.cached_voices if v.voice_id == result.voice_id),
                tier,
            )
    async with session_scope() as session:
        cost, free = await ctx.usage.estimate(
            session,
            provider=result.provider,
            tier=tier,
            characters=result.billed_characters or len(text),
        )
        await ctx.usage.record(
            session,
            provider=result.provider,
            voice_id=result.voice_id,
            tier=tier,
            characters=result.billed_characters or len(text),
            cost_micro_usd=cost,
            free_tier=free,
            cached=False,
            api_key_id=api_key_id,
            job_id=job_id,
        )
    result = result.model_copy(update={"cost_micro_usd": cost, "used_free_tier": free})
    meta["tier"] = tier
    meta["estimated_cost_micro_usd"] = cost

    # --- cache write ---------------------------------------------------------------------
    if ctx.cache.enabled and not bypass_cache:
        await ctx.cache.put(
            cache_key,
            result.audio,
            fmt=result.format,
            provider=result.provider,
            voice_id=result.voice_id,
            meta={
                "used_provider": result.provider,
                "sample_rate": result.sample_rate,
                "duration_ms": duration,
                # Timings are produced during synthesis and cost nothing to store. Without
                # them a cache hit answers with audio but no subtitles, so the same request
                # returns a different shape the second time.
                "segments": [seg.model_dump() for seg in (result.segments or [])],
                "tier": meta.get("tier"),
            },
        )

    # --- history ----------------------------------------------------------------------------
    audio_path: str | None = None
    history_id = None  # may already be set by the cache branch above
    if persist:
        history_id, audio_path = await _persist(
            ctx,
            req,
            result,
            meta,
            result.audio,
            duration,
            chunks=len(plan.chunks),
            normalized=plan.normalized,
            api_key_id=api_key_id,
            client=client,
        )

    return SynthesisOutcome(
        result=result,
        meta=meta,
        history_id=history_id,
        audio_path=audio_path,
        chunks=len(plan.chunks),
        normalized_text=plan.normalized,
        duration_ms=duration,
    )


async def _persist(
    ctx: AppContext,
    req: SynthRequest,
    result: SynthResult,
    meta: dict[str, Any],
    audio: bytes,
    duration_ms: int | None,
    *,
    chunks: int,
    normalized: str,
    api_key_id: str | None,
    client: str | None,
) -> tuple[str, str]:
    record_id = uuid.uuid4().hex
    audio_path = None
    if audio:
        path = ctx.settings.data_path / "audio" / f"{record_id}.{result.format.value}"
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.write_bytes, audio)
        audio_path = str(path)

    async with session_scope() as session:
        session.add(
            SynthRecord(
                id=record_id,
                text=req.text[:20000],
                text_chars=len(req.text),
                provider=result.provider,
                voice_id=result.voice_id,
                requested_provider=req.provider,
                params={
                    "format": req.format.value,
                    "sample_rate": req.sample_rate,
                    "speed": req.speed,
                    "pitch": req.pitch,
                    "volume": req.volume,
                    "ssml": req.ssml,
                    "style": req.style,
                    "prompt": req.prompt,
                    "language": req.language,
                    "chunks": chunks,
                    "normalized_text": normalized if normalized != req.text else None,
                    "fallback": meta.get("fallback_used"),
                    "attempts": meta.get("attempts"),
                    "loudness": req.loudness,
                },
                format=result.format.value,
                sample_rate=result.sample_rate,
                audio_path=audio_path,
                audio_bytes=len(result.audio),
                duration_ms=duration_ms,
                latency_ms=result.latency_ms,
                first_byte_ms=result.first_byte_ms,
                segments=[seg.model_dump() for seg in (result.segments or [])],
                from_cache=result.from_cache,
                unsupported=result.unsupported,
                cost_micro_usd=result.cost_micro_usd,
                billed_chars=result.billed_characters,
                used_free_tier=result.used_free_tier,
                api_key_id=api_key_id,
                client=client,
            )
        )
    return record_id, audio_path or ""


def estimate_preview(req: SynthRequest) -> dict[str, Any]:
    seconds = estimate_speech_seconds(req.text, req.speed)
    return {"estimated_duration_ms": int(seconds * 1000), "characters": len(req.text)}
