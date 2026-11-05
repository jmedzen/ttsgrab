"""Worker registry endpoints + reference-audio management."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, UploadFile
from sqlalchemy import delete, select

from ttsgrab.api.deps import Ctx, PrincipalDep, write_audit
from ttsgrab.api.schemas import WorkerIn, WorkerOut
from ttsgrab.core.errors import InvalidRequest, NotFound
from ttsgrab.core.security import SsrfGuard, validate_upload
from ttsgrab.db.models import ReferenceAudio, WorkerNode
from ttsgrab.db.session import session_scope
from ttsgrab.workers.client import WorkerProvider

router = APIRouter(tags=["workers"])


def _worker_out(node: WorkerNode, ctx) -> WorkerOut:
    provider = ctx.registry.get(node.name)
    return WorkerOut(
        id=node.id,
        name=node.name,
        base_url=node.base_url,
        kind=node.kind,
        enabled=node.enabled,
        healthy=node.healthy,
        consecutive_failures=node.consecutive_failures,
        last_checked_at=node.last_checked_at.isoformat() if node.last_checked_at else None,
        last_error=node.last_error,
        latency_ms_ewma=round(node.latency_ms_ewma, 1) if node.latency_ms_ewma else None,
        voices=len(provider.cached_voices) if provider else 0,
        model_hint=node.model_hint,
    )


@router.get("/v1/workers", summary="List registered local workers")
async def list_workers(ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    async with session_scope() as session:
        rows = (await session.execute(select(WorkerNode).order_by(WorkerNode.name))).scalars().all()
        return {"workers": [_worker_out(n, ctx).model_dump() for n in rows]}


@router.post("/v1/workers", summary="Register a local worker by base_url")
async def add_worker(body: WorkerIn, ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    guard = SsrfGuard(
        allow_private=ctx.settings.security.allow_private_worker_urls,
        allowed_hosts=ctx.settings.security.allowed_worker_hosts,
    )
    url = guard.validate(body.base_url)

    async with session_scope() as session:
        existing = (
            await session.execute(select(WorkerNode).where(WorkerNode.name == body.name))
        ).scalar_one_or_none()
        if existing is not None:
            raise InvalidRequest(f"worker {body.name!r} already registered")
        node = WorkerNode(
            name=body.name,
            base_url=url,
            kind=body.kind,
            enabled=body.enabled,
            model_hint=body.model_hint,
            price_micro_usd_per_mchar=body.price_micro_usd_per_mchar,
            auth_header=body.auth_header,
        )
        session.add(node)
        await session.flush()
        await session.refresh(node)

    provider = WorkerProvider(node, ctx.settings)
    ctx.registry.add_instance(provider)
    try:
        await provider.refresh_voices()
        node.healthy = True
    except Exception as exc:
        node.healthy = False
        node.last_error = f"{type(exc).__name__}: {exc}"[:500]
    async with session_scope() as session:
        merged = await session.merge(node)
        await session.flush()
        await session.refresh(merged)
        out = _worker_out(merged, ctx).model_dump()

    await write_audit(
        ctx,
        principal=principal,
        action="worker.register",
        target=body.name,
        detail={"base_url": url, "kind": body.kind},
    )
    return out


@router.post("/v1/workers/{worker_id}/check", summary="Run an immediate health check")
async def check_worker(worker_id: str, ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    from ttsgrab.workers.monitor import WorkerMonitor

    monitor: WorkerMonitor | None = ctx.extras.get("worker_monitor")
    if monitor is None:
        monitor = WorkerMonitor(ctx)
        ctx.extras["worker_monitor"] = monitor
    node = await monitor.check_one(worker_id)
    if node is None:
        raise NotFound(f"worker {worker_id} not found")
    async with session_scope() as session:
        fresh = await session.get(WorkerNode, worker_id)
        if fresh is None:
            raise NotFound(f"worker {worker_id} not found")
        return _worker_out(fresh, ctx).model_dump()


@router.delete("/v1/workers/{worker_id}", summary="Remove a worker")
async def delete_worker(worker_id: str, ctx: Ctx, principal: PrincipalDep) -> dict[str, str]:
    async with session_scope() as session:
        node = await session.get(WorkerNode, worker_id)
        if node is None:
            raise NotFound(f"worker {worker_id} not found")
        name = node.name
        await session.execute(delete(WorkerNode).where(WorkerNode.id == worker_id))
    ctx.registry._instances.pop(name, None)
    await write_audit(ctx, principal=principal, action="worker.delete", target=name)
    return {"deleted": worker_id}


# -- reference audio (voice cloning) ------------------------------------------------------


def _ref_out(ref: ReferenceAudio) -> dict[str, Any]:
    return {
        "id": ref.id,
        "name": ref.name,
        "provider": ref.provider,
        "mime": ref.mime,
        "bytes": ref.bytes,
        "duration_ms": ref.duration_ms,
        "transcript": ref.transcript,
        "consent_note": ref.consent_note,
        "created_at": ref.created_at.isoformat(),
    }


@router.get("/v1/references", summary="List uploaded reference clips")
async def list_references(ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(ReferenceAudio).order_by(ReferenceAudio.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        return {"references": [_ref_out(r) for r in rows]}


@router.post("/v1/references", summary="Upload a reference clip for voice cloning")
async def upload_reference(
    ctx: Ctx,
    principal: PrincipalDep,
    file: UploadFile = File(...),
    name: str = Form(...),
    provider: str = Form(...),
    transcript: str | None = Form(default=None),
    consent_note: str | None = Form(default=None),
) -> dict[str, Any]:
    """Upload a reference clip.

    Only upload audio you have the right to use. Cloning someone else's voice without
    their consent is illegal in many jurisdictions (EU AI Act Art. 50, Taiwan's
    Personal Data Protection Act) and is not what this feature is for.
    """
    data = await file.read()
    mime = validate_upload(
        data,
        declared_mime=file.content_type,
        max_bytes=ctx.settings.security.max_upload_bytes,
        allowed=ctx.settings.security.allowed_upload_mimes,
    )
    if not consent_note or not consent_note.strip():
        raise InvalidRequest(
            "`consent_note` is required: describe why you may clone this voice "
            "(e.g. 'my own voice', 'written permission from <name> on <date>')"
        )

    import uuid

    from ttsgrab.core.audio import probe_duration_ms, sniff_format

    ref_id = uuid.uuid4().hex
    ext = {
        "audio/wav": "wav",
        "audio/mpeg": "mp3",
        "audio/flac": "flac",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "audio/mp4": "m4a",
    }.get(mime, "bin")
    path = ctx.settings.data_path / "reference" / f"{ref_id}.{ext}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)

    detected = sniff_format(data)
    duration = await probe_duration_ms(data, detected) if detected else None

    async with session_scope() as session:
        existing = (
            await session.execute(select(ReferenceAudio).where(ReferenceAudio.name == name))
        ).scalar_one_or_none()
        if existing is not None:
            raise InvalidRequest(f"a reference named {name!r} already exists")
        ref = ReferenceAudio(
            id=ref_id,
            name=name,
            provider=provider,
            path=str(path),
            mime=mime,
            bytes=len(data),
            duration_ms=duration,
            transcript=transcript,
            consent_note=consent_note,
        )
        session.add(ref)
        await session.flush()
        out = _ref_out(ref)

    await write_audit(
        ctx,
        principal=principal,
        action="reference.upload",
        target=name,
        detail={"bytes": len(data), "provider": provider},
    )
    return out


@router.get("/v1/references/{ref_id}/audio", summary="Fetch a reference clip")
async def get_reference_audio(ref_id: str, ctx: Ctx) -> Any:
    from fastapi.responses import FileResponse

    async with session_scope() as session:
        ref = await session.get(ReferenceAudio, ref_id)
        if ref is None:
            raise NotFound(f"reference {ref_id} not found")
        path, mime = Path(ref.path), ref.mime
    if not path.is_file():  # noqa: ASYNC240 - single stat on a local file
        raise NotFound("reference audio file missing from disk")
    return FileResponse(path, media_type=mime)


@router.delete("/v1/references/{ref_id}", summary="Delete a reference clip")
async def delete_reference(ref_id: str, ctx: Ctx, principal: PrincipalDep) -> dict[str, str]:
    async with session_scope() as session:
        ref = await session.get(ReferenceAudio, ref_id)
        if ref is None:
            raise NotFound(f"reference {ref_id} not found")
        Path(ref.path).unlink(missing_ok=True)  # noqa: ASYNC240 - single unlink
        await session.execute(delete(ReferenceAudio).where(ReferenceAudio.id == ref_id))
    await write_audit(ctx, principal=principal, action="reference.delete", target=ref_id)
    return {"deleted": ref_id}
