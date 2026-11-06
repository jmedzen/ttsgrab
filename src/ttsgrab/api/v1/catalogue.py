"""Catalogue and provider introspection endpoints."""

from __future__ import annotations

import contextlib
from typing import Any

from fastapi import APIRouter, Query

from ttsgrab.api.deps import AdminDep, Ctx, PrincipalDep, write_audit
from ttsgrab.api.schemas import ProviderOut, VoiceOut
from ttsgrab.core.errors import InvalidRequest, NotFound
from ttsgrab.core.obs import PROVIDER_UP
from ttsgrab.core.types import CostClass

router = APIRouter(tags=["catalogue"])


def _voice_out(ctx, voice) -> VoiceOut:
    spec = ctx.pricing.tier(voice.provider, voice.tier)
    return VoiceOut(
        provider=voice.provider,
        voice_id=voice.voice_id,
        name=voice.name,
        languages=voice.languages,
        gender=voice.gender,
        styles=voice.styles,
        cost_class=voice.cost_class.value,
        tier=voice.tier,
        streaming=voice.streaming,
        cloning=voice.cloning,
        ssml=voice.ssml,
        price_usd_per_mchar=spec.usd_per_mchar,
        free_chars_per_month=spec.free_chars_per_month,
    )


@router.get("/v1/voices", summary="List every voice across all providers")
async def list_voices(
    ctx: Ctx,
    principal: PrincipalDep,
    provider: str | None = Query(default=None, description="Filter by provider name."),
    language: str | None = Query(default=None, description="BCP-47 prefix, e.g. 'zh' or 'zh-TW'."),
    gender: str | None = Query(default=None),
    search: str | None = Query(default=None, description="Substring match on name or voice id."),
    q: str | None = Query(default=None, description="Alias for `search`."),
    free_only: bool = Query(default=False, description="Only zero-cost voices."),
    cloning: bool | None = Query(default=None, description="Only voices supporting cloning."),
    streaming: bool | None = Query(default=None),
    limit: int = Query(default=2000, ge=1, le=10000),
    offset: int = Query(default=0, ge=0),
) -> dict[str, Any]:
    voices = ctx.registry.catalogue()
    if principal.provider_allowlist:
        voices = [v for v in voices if v.provider in principal.provider_allowlist]
    if provider:
        voices = [v for v in voices if v.provider == provider]
    if language:
        needle = language.lower()
        voices = [v for v in voices if any(lg.lower().startswith(needle) for lg in v.languages)]
    if gender:
        voices = [v for v in voices if v.gender == gender]
    needle_text = (search or q or "").lower()
    if needle_text:
        voices = [
            v
            for v in voices
            if needle_text in v.voice_id.lower()
            or needle_text in v.name.lower()
            or any(needle_text in lg.lower() for lg in v.languages)
        ]
    if free_only:
        voices = [v for v in voices if v.cost_class == CostClass.FREE]
    if cloning is not None:
        voices = [v for v in voices if v.cloning is cloning]
    if streaming is not None:
        voices = [v for v in voices if v.streaming is streaming]

    total = len(voices)
    page = voices[offset : offset + limit]
    return {
        "total": total,
        "count": len(page),
        "voices": [_voice_out(ctx, v).model_dump() for v in page],
        "languages": sorted({lg for v in voices for lg in v.languages}),
        "providers": sorted({v.provider for v in voices}),
    }


@router.get("/v1/providers", summary="Provider status, health and capability summary")
async def list_providers(ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    snapshot = ctx.orchestrator.snapshot()
    out: list[ProviderOut] = []
    for provider in ctx.registry.all():
        status = provider.status()
        health = snapshot.get(provider.name, {})
        with contextlib.suppress(Exception):
            PROVIDER_UP.labels(provider.name).set(1 if provider.enabled else 0)
        out.append(
            ProviderOut(
                name=status.name,
                enabled=status.enabled,
                kind=status.kind,
                official=status.official,
                stability=status.stability.value,
                cost_class=status.cost_class.value,
                reason=status.reason or ctx.registry.errors().get(provider.name),
                note=status.note,
                tos_url=status.tos_url,
                voices=len(provider.cached_voices),
                health=health,
            )
        )
    return {
        "providers": [p.model_dump() for p in out],
        "errors": ctx.registry.errors(),
        "pricing_version": ctx.pricing.version,
        "pricing_effective": ctx.pricing.effective,
    }


@router.post(
    "/v1/providers/{name}/refresh",
    summary="Re-fetch a provider's voice list without restarting",
)
async def refresh_provider(name: str, ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    """Most common use: credentials were added after boot.

    Providers are built once at startup, so a key added later is invisible until the
    process restarts. This rebuilds just that one adapter and re-fetches its voices.
    """
    from ttsgrab.core.registry import load_builtin_providers, registered_classes

    old = ctx.registry.get(name)
    load_builtin_providers()
    cls = registered_classes().get(name)
    if cls is None:
        raise NotFound(f"provider {name!r} is not registered")

    try:
        provider = cls(ctx.settings)
    except Exception as exc:
        raise InvalidRequest(f"cannot rebuild provider {name!r}: {exc}") from exc

    if not provider.enabled:
        # Replacing a working adapter with a disabled one would be a regression.
        return {
            "name": name,
            "enabled": False,
            "reason": provider.disabled_reason,
            "voices": len(old.cached_voices) if old else 0,
            "reloaded": False,
        }

    # `startup()` may be a no-op (edge does its own lazy fetch), so ask explicitly for the
    # voice list. Swapping in an adapter with an empty catalogue would silently remove a
    # working provider from routing.
    try:
        await provider.refresh_voices()
    except Exception as exc:
        kept = len(old.cached_voices) if old is not None else 0
        raise InvalidRequest(
            f"provider {name!r} could not be refreshed: {exc}. "
            f"The previously loaded adapter is still in use ({kept} voices)."
        ) from exc

    ctx.registry.add_instance(provider)
    await write_audit(ctx, principal=principal, action="provider.refresh", target=name)
    return {
        "name": name,
        "enabled": True,
        "reason": None,
        "voices": len(provider.cached_voices),
        "reloaded": True,
    }


@router.get("/v1/capabilities", summary="What the gateway supports, for client discovery")
async def capabilities(ctx: Ctx) -> dict[str, Any]:
    from ttsgrab.buildinfo import build_info
    from ttsgrab.core.audio import has_ffmpeg
    from ttsgrab.core.textsplit import has_opencc

    return {
        # A client that caches capabilities needs to know when they changed.
        **build_info().as_dict(),
        "formats": ["mp3", "wav", "opus", "flac"],
        "sample_rates": [8000, 16000, 22050, 24000, 32000, 44100, 48000],
        "policies": ["cheapest", "fastest", "best_quality", "local_only", "free_only", "explicit"],
        "max_text_chars": ctx.settings.security.max_text_chars,
        "ffmpeg": has_ffmpeg(),
        "opencc": has_opencc(),
        "streaming": True,
        "openai_compatible": "/v1/audio/speech",
        "mcp": "/mcp",
    }


# -- Google AI Studio key pool --------------------------------------------------------------
# Keys are managed at runtime because the whole point of the free tier is that it is small:
# an operator adds a second free key, or a paid one, without editing .env and restarting.
# Every route here is admin-only — a key pool is a billing surface.


def _studio(ctx):
    provider = ctx.registry.get("google_ai_studio")
    if provider is None:
        raise NotFound("google_ai_studio provider is not registered")
    return provider


@router.get(
    "/v1/providers/google_ai_studio/keys",
    summary="List the Google AI Studio key pool (labels and counters, never secrets)",
)
async def list_studio_keys(ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    provider = _studio(ctx)
    status = provider.pool_status()
    status["enabled"] = provider.enabled
    status["reason"] = provider.disabled_reason
    return status


@router.post(
    "/v1/providers/google_ai_studio/keys",
    summary="Add a free or paid Google AI Studio API key",
)
async def add_studio_key(body: dict[str, Any], ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    provider = _studio(ctx)
    secret = (body.get("key") or "").strip()
    kind = (body.get("kind") or "free").strip()
    if not secret:
        raise InvalidRequest("`key` is required")
    if kind not in ("free", "paid"):
        raise InvalidRequest("`kind` must be 'free' or 'paid'")
    try:
        label = provider.add_key(secret, kind=kind)
    except ValueError as exc:
        # Duplicate key or empty key: both are user errors, not outages.
        raise InvalidRequest(str(exc)) from exc
    await write_audit(
        ctx,
        principal=principal,
        action="provider.key_added",
        target="google_ai_studio",
        detail={"label": label, "kind": kind},
    )
    await _refresh_studio(ctx)
    return {"label": label, "kind": kind, **provider.pool_status()}


@router.delete(
    "/v1/providers/google_ai_studio/keys/{label}",
    summary="Remove a key from the pool by its label",
)
async def delete_studio_key(label: str, ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    provider = _studio(ctx)
    if not provider.remove_key(label):
        raise NotFound(f"no key labelled {label!r}")
    await write_audit(
        ctx,
        principal=principal,
        action="provider.key_removed",
        target="google_ai_studio",
        detail={"label": label},
    )
    await _refresh_studio(ctx)
    return {"removed": label, **provider.pool_status()}


async def _refresh_studio(ctx) -> None:
    """Re-fetch voices so a newly enabled provider appears without a restart."""
    provider = ctx.registry.get("google_ai_studio")
    if provider is None:
        return
    with contextlib.suppress(Exception):
        await provider.refresh_voices()
