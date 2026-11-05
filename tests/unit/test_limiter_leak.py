"""The rate limiter must not grow without bound.

Its state is one dict entry per principal that ever made a request, and the key is derived
from caller-controlled input. Left unswept that is a slow, caller-driven memory leak.
"""

from __future__ import annotations

import time

from ttsgrab.core.security import SlidingWindowLimiter


def test_distinct_keys_do_not_accumulate_forever() -> None:
    limiter = SlidingWindowLimiter()
    limiter._sweep_interval_s = 0  # sweep on every check, so the test is deterministic

    for i in range(500):
        limiter.check(f"burst-{i}", 10)
    assert len(limiter._hits) == 500

    # Every existing bucket ages out.
    stale = time.monotonic() - 3600
    for bucket in limiter._hits.values():
        bucket.clear()
        bucket.append(stale)

    limiter.check("fresh", 10)
    assert len(limiter._hits) <= 2, "stale buckets were never reclaimed"


def test_sweeping_does_not_reset_a_live_limit() -> None:
    """The fix must not hand an attacker a fresh window by evicting a live bucket."""
    limiter = SlidingWindowLimiter()
    limiter._sweep_interval_s = 0

    for _ in range(3):
        assert limiter.check("hot", 3)[0] is True

    allowed, retry_after = limiter.check("hot", 3)
    assert allowed is False
    assert retry_after >= 1


def test_the_window_still_expires() -> None:
    limiter = SlidingWindowLimiter()
    limiter._sweep_interval_s = 0
    for _ in range(2):
        limiter.check("w", 2)
    assert limiter.check("w", 2)[0] is False

    # Rewind the recorded hits past the window; the limiter must let the caller back in.
    stale = time.monotonic() - 120
    for bucket in limiter._hits.values():
        for i in range(len(bucket)):
            bucket[i] = stale

    assert limiter.check("w", 2)[0] is True
