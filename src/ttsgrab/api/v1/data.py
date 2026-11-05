"""History, usage and audio retrieval."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select

from ttsgrab.api.deps import Ctx, PrincipalDep
from ttsgrab.api.schemas import HistoryItem, UsageOut
from ttsgrab.core.errors import NotFound
from ttsgrab.db.models import SynthRecord
from ttsgrab.db.session import session_scope

router = APIRouter(tags=["data"])


@router.get("/v1/history", summary="Synthesis history")
async def history(
    ctx: Ctx,
    principal: PrincipalDep,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    provider: str | None = None,
    q: str | None = Query(default=None, description="Substring match on the source text."),
) -> dict[str, Any]:
    async with session_scope() as session:
        stmt = select(SynthRecord).order_by(SynthRecord.created_at.desc())
        count_stmt = select(func.count()).select_from(SynthRecord)
        if provider:
            stmt = stmt.where(SynthRecord.provider == provider)
            count_stmt = count_stmt.where(SynthRecord.provider == provider)
        if q:
            stmt = stmt.where(SynthRecord.text.contains(q))
            count_stmt = count_stmt.where(SynthRecord.text.contains(q))
        total = (await session.execute(count_stmt)).scalar_one()
        rows = (await session.execute(stmt.offset(offset).limit(limit))).scalars().all()
        items = [
            HistoryItem(
                id=r.id,
                created_at=r.created_at.isoformat(),
                text=r.text,
                text_chars=r.text_chars,
                provider=r.provider,
                voice_id=r.voice_id,
                requested_provider=r.requested_provider,
                format=r.format,
                latency_ms=r.latency_ms,
                duration_ms=r.duration_ms,
                audio_bytes=r.audio_bytes,
                cost_micro_usd=r.cost_micro_usd,
                audio_url=f"/v1/history/{r.id}/audio" if r.audio_path else None,
                params=r.params or {},
                from_cache=r.from_cache,
            ).model_dump()
            for r in rows
        ]
    return {"total": total, "count": len(items), "items": items}


@router.get("/v1/history/{record_id}/audio", summary="Download a historical audio file")
async def history_audio(record_id: str, ctx: Ctx, principal: PrincipalDep) -> FileResponse:
    async with session_scope() as session:
        row = await session.get(SynthRecord, record_id)
        if row is None or not row.audio_path:
            raise NotFound(f"no audio for history record {record_id}")
        path = Path(row.audio_path)
        fmt = row.format
    # A single stat on a local file: microseconds, and it must happen before we hand a
    # path to FileResponse. Offloading it to a thread would cost more than it saves.
    if not path.is_file():  # noqa: ASYNC240
        raise NotFound("audio file was deleted from disk")
    mime = {
        "mp3": "audio/mpeg",
        "wav": "audio/wav",
        "opus": "audio/ogg",
        "flac": "audio/flac",
    }.get(fmt, "application/octet-stream")
    return FileResponse(path, media_type=mime, filename=f"{record_id}.{fmt}")


@router.delete("/v1/history/{record_id}", summary="Delete a history record and its audio")
async def delete_history(record_id: str, ctx: Ctx, principal: PrincipalDep) -> dict[str, str]:
    from ttsgrab.api.deps import write_audit

    async with session_scope() as session:
        row = await session.get(SynthRecord, record_id)
        if row is None:
            raise NotFound(f"history record {record_id} not found")
        if row.audio_path:
            Path(row.audio_path).unlink(missing_ok=True)  # noqa: ASYNC240 - single unlink
        await session.execute(delete(SynthRecord).where(SynthRecord.id == record_id))
    await write_audit(ctx, principal=principal, action="history.delete", target=record_id)
    return {"deleted": record_id}


@router.get("/v1/usage", summary="Usage and cost for the current month", response_model=UsageOut)
async def usage(
    ctx: Ctx,
    principal: PrincipalDep,
    days: int = Query(default=30, ge=1, le=365),
) -> UsageOut:
    async with session_scope() as session:
        summary = await ctx.usage.month_summary(session)
        summary["daily"] = await ctx.usage.daily_series(session, days=days)
    return UsageOut(**summary)


@router.get("/v1/usage/series", summary="Daily characters and cost series")
async def usage_series(
    ctx: Ctx, principal: PrincipalDep, days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    async with session_scope() as session:
        return {"days": await ctx.usage.daily_series(session, days=days)}


@router.get("/v1/usage/free-tier", summary="Remaining free allowance per provider tier")
async def free_tier(ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    async with session_scope() as session:
        return {"free_tier": await ctx.usage.free_tier_status(session)}


@router.get("/v1/stats", summary="Aggregate stats for the dashboard")
async def stats(ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    async with session_scope() as session:
        since = dt.datetime.now(dt.UTC) - dt.timedelta(days=7)
        total = (await session.execute(select(func.count()).select_from(SynthRecord))).scalar_one()
        recent = (
            await session.execute(
                select(func.count()).select_from(SynthRecord).where(SynthRecord.created_at >= since)
            )
        ).scalar_one()
        avg_latency: float | None = (
            await session.execute(
                select(func.avg(SynthRecord.latency_ms)).where(SynthRecord.latency_ms.isnot(None))
            )
        ).scalar_one()
    return {
        "history_total": total,
        "history_last_7d": recent,
        "avg_latency_ms": int(avg_latency) if avg_latency else None,
        "cache": ctx.cache.stats(),
        "voices": len(ctx.registry.catalogue()),
        "providers_enabled": len(ctx.registry.enabled()),
    }
