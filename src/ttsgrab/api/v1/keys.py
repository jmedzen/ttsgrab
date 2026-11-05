"""API key management (admin only)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import select

from ttsgrab.api.deps import AdminDep, Ctx, write_audit
from ttsgrab.api.schemas import ApiKeyCreate
from ttsgrab.core.errors import NotFound
from ttsgrab.core.security import generate_api_key
from ttsgrab.db.models import ApiKey
from ttsgrab.db.session import session_scope

router = APIRouter(tags=["keys"])


def _out(row: ApiKey) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "prefix": row.prefix,
        "created_at": row.created_at.isoformat(),
        "last_used_at": row.last_used_at.isoformat() if row.last_used_at else None,
        "revoked": row.revoked,
        "provider_allowlist": list(row.provider_allowlist or []),
        "rate_limit_per_minute": row.rate_limit_per_minute,
        "monthly_char_quota": row.monthly_char_quota,
    }


@router.get("/v1/keys", summary="List API keys (never returns the secret)")
async def list_keys(ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    async with session_scope() as session:
        rows = (
            (await session.execute(select(ApiKey).order_by(ApiKey.created_at.desc())))
            .scalars()
            .all()
        )
        return {"keys": [_out(r) for r in rows]}


@router.post("/v1/keys", summary="Create an API key — the token is returned only once")
async def create_key(body: ApiKeyCreate, ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    raw, key_hash, prefix = generate_api_key()
    async with session_scope() as session:
        row = ApiKey(
            name=body.name,
            key_hash=key_hash,
            prefix=prefix,
            provider_allowlist=body.provider_allowlist,
            rate_limit_per_minute=body.rate_limit_per_minute,
            monthly_char_quota=body.monthly_char_quota,
            monthly_cost_quota_micro_usd=body.monthly_cost_quota_micro_usd,
            note=body.note,
        )
        session.add(row)
        await session.flush()
        out = _out(row)
    out["token"] = raw
    await write_audit(
        ctx,
        principal=principal,
        action="apikey.create",
        target=body.name,
        detail={"key_id": out["id"], "allowlist": body.provider_allowlist},
    )
    return out


@router.delete("/v1/keys/{key_id}", summary="Revoke an API key")
async def revoke_key(key_id: str, ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    async with session_scope() as session:
        row = await session.get(ApiKey, key_id)
        if row is None:
            raise NotFound(f"API key {key_id} not found")
        row.revoked = True
        await session.flush()
    await write_audit(ctx, principal=principal, action="apikey.revoke", target=key_id)
    return {"revoked": key_id}


@router.get("/v1/audit", summary="Recent audit log entries")
async def audit_log(ctx: Ctx, principal: AdminDep, limit: int = 100) -> dict[str, Any]:
    from ttsgrab.db.models import AuditLog

    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return {
            "entries": [
                {
                    "id": r.id,
                    "created_at": r.created_at.isoformat(),
                    "actor": r.actor,
                    "action": r.action,
                    "target": r.target,
                    "status": r.status,
                    "detail": r.detail,
                    "ip": r.ip,
                }
                for r in rows
            ]
        }
