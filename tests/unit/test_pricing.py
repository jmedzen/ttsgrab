"""Pricing and cost-accounting tests.

These are the numbers that decide whether the gateway protects your wallet, so they get
exact assertions rather than tolerance.
"""

from __future__ import annotations

import pytest

from ttsgrab.core.pricing import (
    DEFAULT_PRICING,
    PricingTable,
    compute_cost_micro_usd,
    month_bounds,
)


@pytest.fixture
def pricing() -> PricingTable:
    return PricingTable(DEFAULT_PRICING)


def test_neural2_under_free_tier_is_free(pricing: PricingTable) -> None:
    cost, free = compute_cost_micro_usd(
        pricing, provider="google_cloud", tier="Neural2", characters=10_000
    )
    assert cost == 0
    assert free is True


def test_neural2_over_free_tier_bills_only_the_excess(pricing: PricingTable) -> None:
    # 1M free. 1.5M chars -> 500k billable at $16/1M = $8 = 8_000_000 micro-USD.
    cost, free = compute_cost_micro_usd(
        pricing,
        provider="google_cloud",
        tier="Neural2",
        characters=500_000,
        already_billed_chars_this_month=1_000_000,
    )
    assert cost == 8_000_000
    assert free is False


def test_request_straddling_the_free_boundary_is_split(pricing: PricingTable) -> None:
    # 100k free chars remain; ask for 200k -> 100k billed = $1.6
    cost, free = compute_cost_micro_usd(
        pricing,
        provider="google_cloud",
        tier="Neural2",
        characters=200_000,
        already_billed_chars_this_month=900_000,
    )
    assert cost == 1_600_000
    assert free is True  # part of it was free


def test_chirp3_hd_is_thirty_usd_per_million(pricing: PricingTable) -> None:
    cost, _ = compute_cost_micro_usd(
        pricing,
        provider="google_cloud",
        tier="Chirp3-HD",
        characters=1_000_000,
        already_billed_chars_this_month=1_000_000,
    )
    assert cost == 30_000_000


def test_studio_is_the_expensive_one(pricing: PricingTable) -> None:
    cost, _ = compute_cost_micro_usd(
        pricing,
        provider="google_cloud",
        tier="Studio",
        characters=1_000_000,
        already_billed_chars_this_month=1_000_000,
    )
    assert cost == 160_000_000


def test_google_ai_studio_has_no_free_allowance(pricing: PricingTable) -> None:
    cost, free = compute_cost_micro_usd(
        pricing, provider="google_ai_studio", tier="gemini-tts", characters=10_000
    )
    assert cost > 0
    assert free is False


def test_free_providers_never_cost_money(pricing: PricingTable) -> None:
    for provider in ("edge", "kokoro", "gpt_sovits", "cosyvoice"):
        cost, free = compute_cost_micro_usd(
            pricing, provider=provider, tier=None, characters=1_000_000
        )
        assert cost == 0, provider
        assert free is True


def test_respect_free_tier_false_bills_from_the_first_character(pricing: PricingTable) -> None:
    cost, _ = compute_cost_micro_usd(
        pricing,
        provider="google_cloud",
        tier="Neural2",
        characters=100_000,
        respect_free_tier=False,
    )
    assert cost == 1_600_000


def test_unknown_provider_falls_back_to_zero_cost(pricing: PricingTable) -> None:
    cost, free = compute_cost_micro_usd(pricing, provider="mystery", tier=None, characters=1000)
    assert cost == 0
    assert free is True


def test_tier_lookup_is_exact_then_first_match(pricing: PricingTable) -> None:
    assert pricing.tier("google_cloud", "Neural2").usd_per_mchar == 16.0
    # Unknown tier for a known provider → first configured tier, never a crash.
    assert pricing.tier("google_cloud", "Nonsense").provider == "google_cloud"


def test_yaml_overrides_merge_over_defaults(tmp_path) -> None:
    cfg = tmp_path / "pricing.yaml"
    cfg.write_text(
        "version: 99\ntiers:\n  google_cloud:\n    Neural2:\n      usd_per_mchar: 15.0\n",
        encoding="utf-8",
    )
    table = PricingTable.load(cfg)
    assert table.version == 99
    assert table.tier("google_cloud", "Neural2").usd_per_mchar == 15.0
    # Untouched tiers survive the merge.
    assert table.tier("google_cloud", "Chirp3-HD").usd_per_mchar == 30.0


def test_month_bounds_are_calendar_months_utc() -> None:
    import datetime as dt

    start, end = month_bounds(dt.datetime(2026, 2, 15, 12, tzinfo=dt.UTC))
    assert start == dt.datetime(2026, 2, 1, tzinfo=dt.UTC)
    assert end == dt.datetime(2026, 3, 1, tzinfo=dt.UTC)


def test_repo_pricing_yaml_is_found_without_relying_on_cwd(tmp_path, monkeypatch) -> None:
    """A container or systemd unit does not start in the repo, so a CWD-only lookup would
    silently fall back to the built-in table."""
    from ttsgrab.core.context import _find_pricing_file

    monkeypatch.chdir(tmp_path)
    found = _find_pricing_file(None)
    assert found is not None and found.name == "pricing.yaml"


def test_explicit_config_dir_beats_discovery(tmp_path) -> None:
    from ttsgrab.core.context import _find_pricing_file

    custom = tmp_path / "pricing.yaml"
    custom.write_text("version: 99\neffective: '2030-01-01'\ntiers: {}\n", encoding="utf-8")
    assert _find_pricing_file(tmp_path) == custom


def test_no_pricing_file_returns_none(tmp_path, monkeypatch) -> None:
    from ttsgrab.core import context as context_mod

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TTSGRAB_CONFIG_DIR", str(tmp_path / "empty"))
    monkeypatch.setattr(context_mod, "__file__", str(tmp_path / "fake" / "context.py"))
    assert context_mod._find_pricing_file(None) is None
