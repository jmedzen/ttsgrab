"""FastAPI dependencies: context, auth, rate limiting, audit."""

from __future__ import annotations

import datetime as dt
from typing import Annotated

from fastapi import Depends, Header, Request

from ttsgrab.core.context import AppContext
from ttsgrab.core.errors import Forbidden, Unauthorized
from ttsgrab.core.security import Principal, hash_api_key
from ttsgrab.db.models import ApiKey, AuditLog
from ttsgrab.db.session import session_scope

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost", "testclient"}


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _extract_token(request: Request, authorization: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    header = request.headers.get("x-api-key")
    if header:
        return header.strip()
    # EventSource cannot set headers; allow ?api_key= for the streaming UI only.
    if request.url.path.endswith("/stream"):
        return request.query_params.get("api_key")
    return None


async def get_principal(
    request: Request,
    ctx: Ctx,
    authorization: Annotated[str | None, Header()] = None,
) -> Principal:
    """Resolve the caller.

    Order: configured admin token → DB API key → static env API keys → anonymous
    (only when auth is unconfigured and the request is local).
    """
    settings = ctx.settings
    token = _extract_token(request, authorization)
    ip = _client_ip(request)

    if token and settings.security.admin_token and _safe_eq(token, settings.security.admin_token):
        return Principal(kind="admin", name="admin")

    if token:
        key_hash = hash_api_key(token)
        async with session_scope() as session:
            from sqlalchemy import select

            found = (
                await session.execute(
                    select(ApiKey).where(ApiKey.key_hash == key_hash, ApiKey.revoked.is_(False))
                )
            ).scalar_one_or_none()
            if found is not None:
                found.last_used_at = dt.datetime.now(dt.UTC)
                await session.flush()
                return Principal(
                    kind="api_key",
                    id=found.id,
                    name=found.name,
                    provider_allowlist=list(found.provider_allowlist or []),
                    rate_limit_per_minute=found.rate_limit_per_minute,
                    monthly_char_quota=found.monthly_char_quota,
                )
        if token in settings.security.api_keys:
            return Principal(kind="api_key", id=None, name="env-key")

    has_auth_configured = bool(settings.security.api_keys or settings.security.admin_token)
    if not has_auth_configured:
        if settings.security.allow_localhost_without_token and ip in LOCAL_HOSTS:
            return Principal(kind="anonymous", name="localhost")
        # No auth configured but accessed remotely: still allow, but flag it.
        return Principal(kind="anonymous", name="anonymous")

    raise Unauthorized(
        "missing or invalid API key",
        details={"hint": "send `Authorization: Bearer <key>` or `X-API-Key`"},
    )


def _safe_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


PrincipalDep = Annotated[Principal, Depends(get_principal)]


async def require_admin(
    request: Request, ctx: Ctx, authorization: Annotated[str | None, Header()] = None
) -> Principal:
    """UI-only endpoints: admin token, or localhost when no token is configured."""
    settings = ctx.settings
    token = _extract_token(request, authorization)
    if token and settings.security.admin_token and _safe_eq(token, settings.security.admin_token):
        return Principal(kind="admin", name="admin")
    if not settings.security.admin_token:
        ip = _client_ip(request)
        if settings.security.allow_localhost_without_token and ip in LOCAL_HOSTS:
            return Principal(kind="admin", name="localhost-admin")
        raise Forbidden("admin token required for remote access to admin endpoints")
    raise Unauthorized("admin token required")


AdminDep = Annotated[Principal, Depends(require_admin)]


async def check_rate_limit(ctx: Ctx, principal: Principal, bucket: str = "default") -> None:
    settings = ctx.settings
    limit = principal.rate_limit_per_minute or settings.security.rate_limit_per_minute
    if limit <= 0:
        return
    limiter = ctx.extras.setdefault("_limiter", None)
    if limiter is None:
        from ttsgrab.core.security import SlidingWindowLimiter

        limiter = SlidingWindowLimiter()
        ctx.extras["_limiter"] = limiter
    ok, retry_after = limiter.check(f"{principal.id or principal.name}:{bucket}", limit)
    if not ok:
        from ttsgrab.core.errors import RateLimited

        raise RateLimited(
            f"rate limit of {limit}/min exceeded",
            details={"retry_after_seconds": retry_after, "limit": limit},
        )


async def write_audit(
    ctx: AppContext,
    *,
    principal: Principal | None,
    action: str,
    target: str | None = None,
    status: str = "ok",
    detail: dict | None = None,
    ip: str | None = None,
) -> None:
    actor = "system"
    if principal is not None:
        actor = (
            f"api_key:{principal.id}"
            if principal.kind == "api_key" and principal.id
            else principal.kind
        )
    async with session_scope() as session:
        from ttsgrab.core.security import audit_detail

        session.add(
            AuditLog(
                actor=actor,
                action=action,
                target=target,
                status=status,
                detail=audit_detail(**(detail or {})),
                ip=ip,
            )
        )
