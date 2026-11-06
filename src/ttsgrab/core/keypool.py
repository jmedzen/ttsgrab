"""A free/paid API-key pool with daily quota accounting.

Why this is its own module
--------------------------
Google AI Studio keys come in two flavours that behave differently under load: keys on the
free tier have a small *daily request* allowance (Gemini TTS is 10 RPD at the time of
writing) and keys with billing attached have a much larger one. Treating them as one list
means either wasting the paid key on work the free key could have done, or hammering a free
key until it 429s and then failing the request.

So the pool keeps them apart, spends the free allowance first, and — crucially — remembers
*how many requests it has already spent today*. Without that counter the gateway would keep
choosing a free key that is already exhausted, which looks like "Google is broken" rather
than "we used our ten requests before lunch".

The reset boundary is deliberately 00:00 **Pacific**, not local midnight, because that is
when Google resets the quota. Using the host's local midnight would make the counter reset
at the wrong moment for most of the world.

Secrets never leave this module: callers get labels, counters and booleans, never the key.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

# Gemini TTS free-tier daily allowance. Kept configurable because Google has changed it more
# than once (it dropped to 10 RPD in November 2026) and an operator may have a different
# tier than the default.
DEFAULT_FREE_RPD = 10

KeyKind = Literal["free", "paid"]


def _pacific_now() -> datetime:
    """Current time in the zone Google uses for quota resets."""
    # PDT is UTC-7, PST is UTC-8. Using a fixed offset would be wrong for half the year, so
    # approximate the US Pacific rules rather than pull in a tz database dependency.
    now = datetime.now(UTC)
    # Second Sunday in March → first Sunday in November is DST.
    year = now.year
    march = datetime(year, 3, 8, tzinfo=UTC)
    dst_start = march + timedelta(days=(6 - march.weekday()) % 7, hours=10)
    nov = datetime(year, 11, 1, tzinfo=UTC)
    dst_end = nov + timedelta(days=(6 - nov.weekday()) % 7, hours=9)
    offset = -7 if dst_start <= now < dst_end else -8
    return now + timedelta(hours=offset)


def quota_day() -> str:
    """The quota day key, e.g. ``2026-11-06``, in Pacific time."""
    return _pacific_now().date().isoformat()


@dataclass
class PoolKey:
    """One key's runtime state. The secret is here, but only here."""

    secret: str
    label: str
    kind: KeyKind = "free"
    rpd_limit: int = 0  # 0 = unlimited (paid keys)
    requests_today: int = 0
    day: str = field(default_factory=quota_day)
    inflight: int = 0
    failures: int = 0
    total_requests: int = 0
    last_used: float = 0.0
    cooldown_until: float = 0.0

    def roll_over(self) -> None:
        """Reset the daily counter when the Pacific quota day changes."""
        today = quota_day()
        if self.day != today:
            self.day = today
            self.requests_today = 0

    @property
    def exhausted(self) -> bool:
        if self.rpd_limit <= 0:
            return False
        return self.requests_today >= self.rpd_limit

    def public(self) -> dict[str, Any]:
        """A view safe to serialise to the UI — no secret, ever."""
        return {
            "label": self.label,
            "kind": self.kind,
            "rpd_limit": self.rpd_limit,
            "requests_today": self.requests_today,
            "remaining_today": (
                None if self.rpd_limit <= 0 else max(0, self.rpd_limit - self.requests_today)
            ),
            "exhausted": self.exhausted,
            "failures": self.failures,
            "total_requests": self.total_requests,
            "cooling_down": self.cooldown_until > time.time(),
        }


class KeyPool:
    """Free-first key selection with failover and daily quota accounting."""

    def __init__(
        self,
        *,
        free_keys: Sequence[str] = (),
        paid_keys: Sequence[str] = (),
        free_rpd: int = DEFAULT_FREE_RPD,
        strategy: Literal["free_first", "round_robin", "least_used"] = "free_first",
    ) -> None:
        self._keys: list[PoolKey] = []
        self.strategy = strategy
        self.free_rpd = free_rpd
        self._rr = 0
        for secret in free_keys:
            self.add(secret, kind="free")
        for secret in paid_keys:
            self.add(secret, kind="paid")

    # -- membership ---------------------------------------------------------------------
    def add(self, secret: str, *, kind: KeyKind = "free", label: str | None = None) -> str:
        secret = secret.strip()
        if not secret:
            raise ValueError("empty API key")
        # Duplicates are almost always a copy-paste accident; silently keeping both would
        # double-count the same daily allowance and make the UI lie.
        if any(k.secret == secret for k in self._keys):
            raise ValueError("this key is already in the pool")
        same_kind = sum(1 for k in self._keys if k.kind == kind) + 1
        auto = f"{kind}#{same_kind}"
        key = PoolKey(
            secret=secret,
            label=label or auto,
            kind=kind,
            rpd_limit=self.free_rpd if kind == "free" else 0,
        )
        self._keys.append(key)
        return key.label

    def remove(self, label: str) -> bool:
        before = len(self._keys)
        self._keys = [k for k in self._keys if k.label != label]
        return len(self._keys) != before

    def __len__(self) -> int:
        return len(self._keys)

    @property
    def empty(self) -> bool:
        return not self._keys

    def public(self) -> list[dict[str, Any]]:
        for k in self._keys:
            k.roll_over()
        return [k.public() for k in self._keys]

    def summary(self) -> dict[str, Any]:
        keys = self.public()
        free = [k for k in keys if k["kind"] == "free"]
        paid = [k for k in keys if k["kind"] == "paid"]
        return {
            "free_keys": len(free),
            "paid_keys": len(paid),
            "free_remaining_today": sum(k["remaining_today"] or 0 for k in free),
            "paid_unlimited": bool(paid),
            "exhausted": bool(keys) and all(k["exhausted"] or k["cooling_down"] for k in keys),
        }

    # -- selection ----------------------------------------------------------------------
    def pick(self) -> PoolKey:
        """Choose a key. Free keys are spent before paid ones."""
        now = time.time()
        for k in self._keys:
            k.roll_over()

        usable = [k for k in self._keys if k.cooldown_until <= now]
        if not usable:
            # Everything is cooling down: take whichever recovers soonest so the caller gets
            # a real upstream error rather than a confusing local one.
            return min(self._keys, key=lambda k: k.cooldown_until)

        if self.strategy == "round_robin":
            self._rr += 1
            return usable[self._rr % len(usable)]
        if self.strategy == "least_used":
            return min(usable, key=lambda k: (k.total_requests, k.last_used))

        # free_first (default): use up the free allowance before touching a paid key.
        fresh_free = [k for k in usable if k.kind == "free" and not k.exhausted]
        if fresh_free:
            return min(fresh_free, key=lambda k: (k.requests_today, k.last_used))
        # A free key that is merely unused *today* still beats billing someone.
        paid = [k for k in usable if k.kind == "paid"]
        if paid:
            return min(paid, key=lambda k: (k.inflight, k.last_used))
        # Only exhausted free keys left: return one anyway so the upstream 429 is visible
        # and attributable, instead of pretending the pool is empty.
        return usable[0]

    def record_use(self, key: PoolKey) -> None:
        key.requests_today += 1
        key.total_requests += 1
        key.last_used = time.time()

    def penalise(self, key: PoolKey, *, cooldown_s: float) -> None:
        key.failures += 1
        key.cooldown_until = time.time() + cooldown_s

    @property
    def all_exhausted(self) -> bool:
        for k in self._keys:
            k.roll_over()
        return bool(self._keys) and all(k.exhausted for k in self._keys)
