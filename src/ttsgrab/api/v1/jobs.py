"""Async jobs with optional webhook callbacks.

Why a job queue at all: agent-driven narration is often longer than a single HTTP request
should stay open, and callers want a webhook rather than a poll loop.

Implementation: a DB table plus an in-process asyncio worker. That is enough for a
single-container gateway and avoids forcing a Redis dependency on a personal deployment.
The ``Job`` model is deliberately queue-agnostic so a Redis/RQ backend can be added later
without touching the API surface.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks
from sqlalchemy import select

from ttsgrab.api.deps import Ctx, PrincipalDep, guard_provider, write_audit
from ttsgrab.api.schemas import JobCreate
from ttsgrab.core.errors import GatewayError, InvalidRequest, NotFound
from ttsgrab.core.synthesis import synthesize
from ttsgrab.core.types import AudioFormat, SynthRequest
from ttsgrab.db.models import Job
from ttsgrab.db.session import session_scope

router = APIRouter(tags=["jobs"])
logger = logging.getLogger(__name__)


def _job_out(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "result": job.result,
        "error": job.error,
        "webhook_status": job.webhook_status,
    }


async def _set_status(job_id: str, **fields: Any) -> None:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            return
        for key, value in fields.items():
            setattr(job, key, value)
        job.updated_at = dt.datetime.now(dt.UTC)
        await session.flush()


def _webhook_private_allowed(ctx: Any) -> bool:
    """Private webhook targets are the common case on a LAN (a bot on 192.168.x.x), so the
    operator opts in rather than the gateway guessing."""
    return bool(getattr(ctx.settings.security, "allow_private_webhooks", False))


async def _fire_webhook(
    url: str, payload: dict[str, Any], *, allow_private: bool = False
) -> int | None:
    """POST the result to the caller's URL.

    The URL is caller-supplied, so it is an SSRF surface in exactly the same way a worker
    base_url is: without a check, a job can make the gateway fetch cloud metadata or an
    internal admin panel and report it back in `webhook_status`.
    """
    from ttsgrab.core.security import SsrfGuard

    try:
        SsrfGuard(allow_private=allow_private).validate(url)
    except Exception as exc:
        logger.warning("refusing webhook to %r: %s", url, exc)
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            return resp.status_code
    except httpx.HTTPError as exc:
        logger.warning("webhook delivery failed: %s", exc)
        return None


async def _run_job(ctx, job_id: str) -> None:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None or job.status not in {"queued", "running"}:
            return
        request = dict(job.request)
        webhook_url = job.webhook_url
        job.status = "running"
        job.progress = 0.1
        await session.flush()

    try:
        req = SynthRequest(
            text=request["text"],
            voice_id=request["voice_id"],
            provider=request.get("provider"),
            format=AudioFormat(request.get("format", "mp3")),
            speed=request.get("speed", 1.0),
            pitch=request.get("pitch", 0.0),
            volume=request.get("volume", 1.0),
            language=request.get("language"),
            prompt=request.get("prompt"),
            strict=request.get("strict", False),
        )
        outcome = await synthesize(
            ctx, req, job_id=job_id, policy=request.get("policy"), persist=True
        )
        await _set_status(
            job_id,
            status="succeeded",
            progress=1.0,
            result={
                "provider": outcome.result.provider,
                "voice_id": outcome.result.voice_id,
                "format": outcome.result.format.value,
                "history_id": outcome.history_id,
                "audio_url": f"/v1/history/{outcome.history_id}/audio"
                if outcome.history_id
                else None,
                "duration_ms": outcome.duration_ms,
                "characters": outcome.result.billed_characters,
                "cost_micro_usd": outcome.result.cost_micro_usd,
            },
        )
        status_code = None
        if webhook_url:
            status_code = await _fire_webhook(
                webhook_url,
                {
                    "job_id": job_id,
                    "status": "succeeded",
                    "result": {
                        "provider": outcome.result.provider,
                        "audio_url": f"/v1/history/{outcome.history_id}/audio"
                        if outcome.history_id
                        else None,
                        "duration_ms": outcome.duration_ms,
                    },
                },
                allow_private=_webhook_private_allowed(ctx),
            )
            await _set_status(job_id, webhook_status=status_code)
    except GatewayError as exc:
        await _set_status(
            job_id, status="failed", progress=1.0, error=exc.to_body().model_dump(mode="json")
        )
        if webhook_url:
            code = await _fire_webhook(
                webhook_url,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "error": exc.to_body().model_dump(mode="json"),
                },
                allow_private=_webhook_private_allowed(ctx),
            )
            await _set_status(job_id, webhook_status=code)
    except Exception as exc:
        logger.exception("job %s crashed", job_id)
        await _set_status(
            job_id,
            status="failed",
            progress=1.0,
            error={"code": "internal_error", "message": str(exc), "retryable": True},
        )


@router.post("/v1/jobs", summary="Queue an asynchronous synthesis job")
async def create_job(
    body: JobCreate,
    background: BackgroundTasks,
    ctx: Ctx,
    principal: PrincipalDep,
) -> dict[str, Any]:
    try:
        voice = body.resolved_voice()
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc

    # Check the allowlist NOW, while the principal is available. The worker that runs this
    # job has no principal, so a job queued by a restricted key would otherwise be billed
    # against whichever provider the router chose.
    guard_provider(principal, body.provider)

    # Quota is checked here rather than in the worker: the worker has no principal, and a
    # job that starts only to be rejected has already been reported as "running".
    if principal.id and principal.monthly_char_quota:
        async with session_scope() as _q:
            await ctx.usage.check_key_quota(
                _q,
                api_key_id=principal.id,
                characters=len(body.text),
                quota=principal.monthly_char_quota,
            )

    async with session_scope() as session:
        job = Job(
            request={
                "text": body.text,
                "voice_id": voice,
                "provider": body.provider,
                "format": body.resolved_format().value,
                "speed": body.speed,
                "pitch": body.pitch,
                "volume": body.volume,
                "language": body.language,
                "prompt": body.prompt,
                "strict": body.strict,
                "policy": body.policy,
            },
            status="queued",
            webhook_url=body.webhook_url,
            api_key_id=principal.id,
        )
        session.add(job)
        await session.flush()
        out = _job_out(job)

    background.add_task(_run_job, ctx, out["id"])
    await write_audit(ctx, principal=principal, action="job.create", target=out["id"])
    return out


@router.get("/v1/jobs/{job_id}", summary="Poll a job")
async def get_job(job_id: str, ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    async with session_scope() as session:
        job = await session.get(Job, job_id)
        if job is None:
            raise NotFound(f"job {job_id} not found")
        return _job_out(job)


@router.get("/v1/jobs", summary="List jobs")
async def list_jobs(ctx: Ctx, principal: PrincipalDep, limit: int = 50) -> dict[str, Any]:
    async with session_scope() as session:
        rows = (
            (await session.execute(select(Job).order_by(Job.created_at.desc()).limit(limit)))
            .scalars()
            .all()
        )
        return {"jobs": [_job_out(j) for j in rows]}
