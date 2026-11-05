"""Usage ledger and budget enforcement.

Google Cloud TTS **bills you automatically** once you pass the monthly free allowance —
there is no hard stop upstream. So the gateway keeps its own ledger and enforces the cap
before spending. This is the only thing standing between "free" and "surprise invoice".

The ledger is derived from ``usage_events`` (one row per synthesis), rolled up per
calendar month in UTC. Cached responses are recorded with ``cached=True`` and zero cost so
the UI can show hit rates without inflating spend.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any

from sqlalchemy import Integer, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttsgrab.core.errors import BudgetExceeded, QuotaExceeded
from ttsgrab.core.pricing import PricingTable, compute_cost_micro_usd, month_bounds
from ttsgrab.db.models import UsageEvent


def _as_int(value: Any) -> int:
    """SQL aggregates come back as ``Any``; normalise None and str without guessing."""
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class UsageLedger:
    def __init__(self, pricing: PricingTable, settings) -> None:
        self.pricing = pricing
        self.settings = settings

    # -- rollups ---------------------------------------------------------------------------
    async def month_summary(
        self, session: AsyncSession, now: dt.datetime | None = None
    ) -> dict[str, Any]:
        start, end = month_bounds(now)
        stmt = (
            select(
                UsageEvent.provider,
                UsageEvent.tier,
                func.sum(UsageEvent.characters).label("chars"),
                func.sum(UsageEvent.cost_micro_usd).label("cost"),
                func.count().label("requests"),
                func.sum(func.cast(UsageEvent.cached, Integer)).label("cached"),
            )
            .where(UsageEvent.created_at >= start, UsageEvent.created_at < end)
            .group_by(UsageEvent.provider, UsageEvent.tier)
        )
        rows = (await session.execute(stmt)).all()

        by_provider: dict[str, dict[str, Any]] = defaultdict(
            lambda: {"characters": 0, "cost_micro_usd": 0, "requests": 0, "cached": 0, "tiers": {}}
        )
        for provider, tier, chars, cost, requests, cached in rows:
            p = by_provider[provider]
            p["characters"] += int(chars or 0)
            p["cost_micro_usd"] += int(cost or 0)
            p["requests"] += int(requests or 0)
            p["cached"] += int(cached or 0)
            p["tiers"][tier or "unknown"] = {
                "characters": int(chars or 0),
                "cost_micro_usd": int(cost or 0),
            }

        total_cost = sum(p["cost_micro_usd"] for p in by_provider.values())
        total_chars = sum(p["characters"] for p in by_provider.values())

        return {
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "totals": {
                "characters": total_chars,
                "cost_micro_usd": total_cost,
                "cost_usd": round(total_cost / 1_000_000, 6),
                "requests": sum(p["requests"] for p in by_provider.values()),
                "cached_hits": sum(p["cached"] for p in by_provider.values()),
            },
            "by_provider": {k: v for k, v in sorted(by_provider.items())},
            "free_tier": await self.free_tier_status(session, by_provider, now),
            "budgets": self.budget_status(by_provider),
        }

    async def provider_chars_this_month(
        self,
        session: AsyncSession,
        provider: str,
        tier: str | None = None,
        now: dt.datetime | None = None,
    ) -> int:
        start, end = month_bounds(now)
        stmt = (
            select(func.coalesce(func.sum(UsageEvent.characters), 0))
            .where(UsageEvent.created_at >= start, UsageEvent.created_at < end)
            .where(UsageEvent.provider == provider)
            .where(UsageEvent.cached.is_(False))
        )
        if tier:
            stmt = stmt.where(UsageEvent.tier == tier)
        return int((await session.execute(stmt)).scalar_one() or 0)

    async def free_tier_status(
        self,
        session: AsyncSession,
        by_provider: dict[str, dict[str, Any]] | None = None,
        now: dt.datetime | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for spec in self.pricing.all_tiers():
            if spec.free_chars_per_month <= 0:
                continue
            used = 0
            if by_provider is not None:
                used = (
                    by_provider.get(spec.provider, {})
                    .get("tiers", {})
                    .get(spec.tier, {})
                    .get("characters", 0)
                )
            else:
                used = await self.provider_chars_this_month(session, spec.provider, spec.tier, now)
            remaining = max(0, spec.free_chars_per_month - used)
            out.setdefault(spec.provider, []).append(
                {
                    "tier": spec.tier,
                    "limit": spec.free_chars_per_month,
                    "used": used,
                    "remaining": remaining,
                    "used_pct": round(used / spec.free_chars_per_month * 100, 1),
                }
            )
        return out

    def budget_status(self, by_provider: dict[str, dict[str, Any]]) -> dict[str, Any]:
        cfg = self.settings
        google = by_provider.get("google_cloud", {})
        gemini = by_provider.get("gemini", {})
        return {
            "google_cloud": {
                "char_budget": cfg.google_cloud.monthly_budget_chars,
                "chars_used": google.get("characters", 0),
                "usd_budget": round(cfg.google_cloud.monthly_budget_micro_usd / 1_000_000, 4),
                "usd_used": round(google.get("cost_micro_usd", 0) / 1_000_000, 4),
            },
            "gemini": {
                "char_budget": cfg.gemini.monthly_budget_chars,
                "chars_used": gemini.get("characters", 0),
                "usd_budget": round(cfg.gemini.monthly_budget_micro_usd / 1_000_000, 4),
                "usd_used": round(gemini.get("cost_micro_usd", 0) / 1_000_000, 4),
            },
        }

    # -- pre-flight ------------------------------------------------------------------------
    async def estimate(
        self,
        session: AsyncSession,
        *,
        provider: str,
        tier: str | None,
        characters: int,
        now: dt.datetime | None = None,
    ) -> tuple[int, bool]:
        used = await self.provider_chars_this_month(session, provider, tier, now)
        return compute_cost_micro_usd(
            self.pricing,
            provider=provider,
            tier=tier,
            characters=characters,
            already_billed_chars_this_month=used,
            respect_free_tier=self.settings.google_cloud.respect_free_tier,
        )

    async def check_budget(
        self,
        session: AsyncSession,
        *,
        provider: str,
        tier: str | None,
        characters: int,
        now: dt.datetime | None = None,
    ) -> tuple[int, bool]:
        """Raise :class:`BudgetExceeded` if this request would breach a configured cap.

        Returns ``(estimated_cost_micro_usd, will_use_free_tier)`` otherwise.

        Per the project decision, a breach is **refused** (HTTP 402), not silently
        downgraded — predictable cost beats availability here.
        """
        cost, free = await self.estimate(
            session, provider=provider, tier=tier, characters=characters, now=now
        )
        summary = await self.month_summary(session, now)

        # One generic check, driven by whatever config section matches the provider name.
        # The previous version special-cased google_cloud and gemini by hand, which meant
        # azure.monthly_budget_chars was documented but never enforced — a spending cap that
        # looked configured and did nothing.
        cfg = getattr(self.settings, provider, None)
        char_budget = int(getattr(cfg, "monthly_budget_chars", 0) or 0)
        usd_budget = int(getattr(cfg, "monthly_budget_micro_usd", 0) or 0)
        bucket = summary["by_provider"].get(provider, {})
        used_chars = int(bucket.get("characters", 0) or 0)
        used_usd = int(bucket.get("cost_micro_usd", 0) or 0)
        fallbacks = [n for n in ("edge", "kokoro", "gpt_sovits") if n != provider]

        if char_budget and used_chars + characters > char_budget:
            raise BudgetExceeded(
                f"{provider} monthly character budget exhausted "
                f"({used_chars}/{char_budget}); refusing to spend",
                details={
                    "provider": provider,
                    "used": used_chars,
                    "budget": char_budget,
                    "requested": characters,
                },
                suggested_fallback=fallbacks,
            )
        if usd_budget and used_usd + cost > usd_budget:
            raise BudgetExceeded(
                f"{provider} monthly USD budget exhausted; refusing to spend",
                details={
                    "provider": provider,
                    "used_micro_usd": used_usd,
                    "budget_micro_usd": usd_budget,
                    "requested_micro_usd": cost,
                },
                suggested_fallback=fallbacks,
            )
        return cost, free

    async def check_key_quota(
        self,
        session: AsyncSession,
        *,
        api_key_id: str | None,
        characters: int,
        quota: int | None,
        now: dt.datetime | None = None,
    ) -> None:
        if not api_key_id or not quota:
            return
        start, end = month_bounds(now)
        stmt = (
            select(func.coalesce(func.sum(UsageEvent.characters), 0))
            .where(UsageEvent.api_key_id == api_key_id)
            .where(UsageEvent.created_at >= start, UsageEvent.created_at < end)
            .where(UsageEvent.cached.is_(False))
        )
        used = int((await session.execute(stmt)).scalar_one() or 0)
        if used + characters > quota:
            raise QuotaExceeded(
                f"API key monthly character quota exceeded ({used}/{quota})",
                details={"used": used, "quota": quota, "requested": characters},
            )

    # -- write -----------------------------------------------------------------------------
    async def record(
        self,
        session: AsyncSession,
        *,
        provider: str,
        voice_id: str,
        tier: str | None,
        characters: int,
        cost_micro_usd: int,
        free_tier: bool,
        cached: bool,
        api_key_id: str | None = None,
        job_id: str | None = None,
    ) -> None:
        session.add(
            UsageEvent(
                provider=provider,
                voice_id=voice_id,
                tier=tier,
                characters=characters,
                cost_micro_usd=cost_micro_usd,
                free_tier=free_tier,
                cached=cached,
                api_key_id=api_key_id,
                job_id=job_id,
            )
        )
        await session.flush()

    async def daily_series(self, session: AsyncSession, days: int = 30) -> list[dict[str, Any]]:
        now = dt.datetime.now(dt.UTC)
        start = (now - dt.timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
        stmt = (
            select(
                func.date(UsageEvent.created_at).label("day"),
                UsageEvent.provider,
                func.sum(UsageEvent.characters),
                func.sum(UsageEvent.cost_micro_usd),
            )
            .where(UsageEvent.created_at >= start)
            .group_by("day", UsageEvent.provider)
            .order_by("day")
        )
        rows = (await session.execute(stmt)).all()
        out: dict[str, dict[str, Any]] = {}
        for day, provider, chars, cost in rows:
            key = str(day)
            bucket = out.setdefault(
                key, {"day": key, "characters": 0, "cost_micro_usd": 0, "providers": {}}
            )
            char_count = _as_int(chars)
            cost_micro = _as_int(cost)
            bucket["characters"] += char_count
            bucket["cost_micro_usd"] += cost_micro
            bucket["providers"][str(provider)] = char_count
        return list(out.values())
