"""The free/paid key pool.

The behaviour worth testing is not "it stores keys" but the two things that are easy to get
wrong and expensive to get wrong in production: burning a paid key while a free allowance is
still unspent, and re-selecting a free key whose daily requests were already used.
"""

from __future__ import annotations

import pytest

from ttsgrab.core.keypool import KeyPool, quota_day


def test_free_keys_are_spent_before_paid_ones() -> None:
    pool = KeyPool(free_keys=["f1"], paid_keys=["p1"], free_rpd=1)
    first = pool.pick()
    pool.record_use(first)
    second = pool.pick()
    assert first.kind == "free", "a paid key was billed while a free allowance was available"
    assert second.kind == "paid"


def test_free_key_is_not_reselected_once_exhausted() -> None:
    """The failure this prevents: ten free requests used, eleventh still picks the free key."""
    pool = KeyPool(free_keys=["f1"], paid_keys=["p1"], free_rpd=2)
    seen: list[str] = []
    for _ in range(3):
        key = pool.pick()
        pool.record_use(key)
        seen.append(key.kind)
    assert seen == ["free", "free", "paid"], f"exhausted free key reused: {seen}"


def test_paid_key_has_no_daily_cap() -> None:
    pool = KeyPool(paid_keys=["p1"], free_rpd=1)
    key = pool.pick()
    for _ in range(50):
        pool.record_use(key)
    assert key.exhausted is False
    assert pool.pick().kind == "paid"


def test_daily_counter_resets_on_a_new_quota_day() -> None:
    pool = KeyPool(free_keys=["f1"], paid_keys=["p1"], free_rpd=1)
    key = pool.pick()
    pool.record_use(key)
    assert key.exhausted is True

    # Simulate the Pacific day rolling over while the process kept running.
    key.day = "1999-01-01"
    key.roll_over()
    assert key.requests_today == 0
    assert key.exhausted is False
    assert pool.pick().kind == "free"


def test_duplicate_key_is_rejected() -> None:
    """Two copies of one key would double-count the same allowance and mislead the UI."""
    pool = KeyPool(free_keys=["same"])
    with pytest.raises(ValueError, match="already in the pool"):
        pool.add("same", kind="free")


def test_empty_key_is_rejected() -> None:
    pool = KeyPool()
    with pytest.raises(ValueError, match="empty"):
        pool.add("   ", kind="free")


def test_secrets_never_appear_in_the_public_view() -> None:
    secret = "AIzaSy-very-secret-value"
    pool = KeyPool(free_keys=[secret], paid_keys=["paid-secret"])
    rendered = repr(pool.public()) + repr(pool.summary())
    assert secret not in rendered
    assert "paid-secret" not in rendered
    # Labels are what the UI shows instead.
    assert pool.public()[0]["label"] == "free#1"


def test_summary_counts_remaining_free_requests() -> None:
    pool = KeyPool(free_keys=["f1", "f2"], free_rpd=10)
    key = pool.pick()
    pool.record_use(key)
    summary = pool.summary()
    assert summary["free_keys"] == 2
    assert summary["paid_keys"] == 0
    assert summary["free_remaining_today"] == 19


def test_a_cooling_key_is_avoided_when_another_works() -> None:
    pool = KeyPool(free_keys=["f1", "f2"])
    first = pool.pick()
    pool.penalise(first, cooldown_s=600)
    second = pool.pick()
    assert second.label != first.label


def test_all_keys_cooling_returns_one_rather_than_raising() -> None:
    """A local 'no keys' error would hide the real upstream failure."""
    pool = KeyPool(free_keys=["f1"])
    key = pool.pick()
    pool.penalise(key, cooldown_s=600)
    assert pool.pick() is key


def test_remove_by_label() -> None:
    pool = KeyPool(free_keys=["f1", "f2"])
    assert pool.remove("free#1") is True
    assert len(pool) == 1
    assert pool.remove("free#9") is False


def test_quota_day_is_a_date_string() -> None:
    # Bound to Pacific time, not local: the counter must reset when Google resets it.
    assert len(quota_day()) == 10
    assert quota_day()[4] == "-"
