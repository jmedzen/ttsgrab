"""Pricing table and cost estimation.

Costs are integers in **micro-USD** (1 µUSD = 0.000001 USD) so monthly sums never drift
and comparisons stay exact.

Prices live in ``pricing.yaml`` (versioned, with an ``effective`` date) rather than in
code, because vendors change them without warning. Values below are the *defaults* used
when the YAML is absent; they were checked against the vendors' public pricing pages in
2026-11 and are documented in docs/providers.md.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

# USD per 1,000,000 characters, converted to micro-USD per 1M chars by *1e6/1e6 = 1*.
# i.e. $16.00 / 1M chars == 16_000_000 µUSD per 1M chars.
MICRO = 1_000_000


@dataclass(frozen=True)
class PriceTier:
    provider: str
    tier: str
    usd_per_mchar: float
    free_chars_per_month: int
    notes: str = ""

    @property
    def micro_per_mchar(self) -> int:
        return round(self.usd_per_mchar * MICRO)


DEFAULT_PRICING: dict[str, Any] = {
    "version": 3,
    "effective": "2026-11-01",
    "currency": "USD",
    "sources": [
        "https://cloud.google.com/text-to-speech/pricing",
        "https://azure.microsoft.com/pricing/details/cognitive-services/speech-services/",
        "https://ai.google.dev/gemini-api/docs/pricing",
    ],
    "tiers": {
        "google_cloud": {
            "Standard": {"usd_per_mchar": 4.0, "free_chars_per_month": 4_000_000},
            "Wavenet": {"usd_per_mchar": 4.0, "free_chars_per_month": 4_000_000},
            "Neural2": {"usd_per_mchar": 16.0, "free_chars_per_month": 1_000_000},
            "Polyglot": {"usd_per_mchar": 16.0, "free_chars_per_month": 1_000_000},
            "Studio": {"usd_per_mchar": 160.0, "free_chars_per_month": 1_000_000},
            "Chirp3-HD": {
                "usd_per_mchar": 30.0,
                "free_chars_per_month": 1_000_000,
                "notes": "Chirp 3 HD",
            },
        },
        "gemini": {
            "gemini-tts": {
                "usd_per_mchar": 12.0,
                "free_chars_per_month": 0,
                "notes": "Gemini 3.8 Flash TTS. No free allowance. Token-priced upstream; "
                "this is a character-equivalent estimate.",
            },
        },
        "azure": {
            "neural": {
                "usd_per_mchar": 16.0,
                "free_chars_per_month": 500_000,
                "notes": "F0 free tier hard-stops at 500k chars/month.",
            },
        },
        "edge": {"neural": {"usd_per_mchar": 0.0, "free_chars_per_month": 0}},
        "gtts": {"translate": {"usd_per_mchar": 0.0, "free_chars_per_month": 0}},
        "translate_tts": {"translate": {"usd_per_mchar": 0.0, "free_chars_per_month": 0}},
        "kokoro": {"local": {"usd_per_mchar": 0.0, "free_chars_per_month": 0}},
        "gpt_sovits": {"local": {"usd_per_mchar": 0.0, "free_chars_per_month": 0}},
        "cosyvoice": {"local": {"usd_per_mchar": 0.0, "free_chars_per_month": 0}},
    },
}


class PricingTable:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.version = int(data.get("version", 0))
        self.effective = str(data.get("effective", ""))
        self._tiers: dict[tuple[str, str], PriceTier] = {}
        for provider, tiers in (data.get("tiers") or {}).items():
            for tier, spec in (tiers or {}).items():
                self._tiers[(provider, tier)] = PriceTier(
                    provider=provider,
                    tier=tier,
                    usd_per_mchar=float(spec.get("usd_per_mchar", 0.0)),
                    free_chars_per_month=int(spec.get("free_chars_per_month", 0)),
                    notes=str(spec.get("notes", "")),
                )

    @classmethod
    def load(cls, path: Path | None = None) -> PricingTable:
        if path is not None and path.is_file():
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            merged = {**DEFAULT_PRICING, **raw}
            # Overlay per-tier so a partial YAML still keeps sane defaults elsewhere.
            tiers = dict(DEFAULT_PRICING["tiers"])
            for provider, spec in (raw.get("tiers") or {}).items():
                tiers[provider] = {**tiers.get(provider, {}), **spec}
            merged["tiers"] = tiers
            return cls(merged)
        return cls(DEFAULT_PRICING)

    def tier(self, provider: str, tier: str | None) -> PriceTier:
        if tier is not None and (provider, tier) in self._tiers:
            return self._tiers[(provider, tier)]
        for (prov, _name), spec in self._tiers.items():
            if prov == provider:
                return spec
        return PriceTier(
            provider=provider, tier=tier or "unknown", usd_per_mchar=0.0, free_chars_per_month=0
        )

    def all_tiers(self) -> list[PriceTier]:
        return sorted(self._tiers.values(), key=lambda t: (t.provider, t.tier))

    def free_allowance(self, provider: str) -> dict[str, int]:
        return {
            tier: spec.free_chars_per_month
            for (prov, tier), spec in self._tiers.items()
            if prov == provider
        }

    def with_overrides(self, overrides: dict[str, Any]) -> PricingTable:
        """Merge ``{"google_cloud": {"Neural2": {"usd_per_mchar": 15}}}}`` over the table."""
        tiers = {
            provider: dict(specs) for provider, specs in (self.data.get("tiers") or {}).items()
        }
        for provider, specs in overrides.items():
            current = tiers.setdefault(provider, {})
            for tier, spec in specs.items():
                current[tier] = {**current.get(tier, {}), **spec}
        return PricingTable({**self.data, "tiers": tiers})


def compute_cost_micro_usd(
    pricing: PricingTable,
    *,
    provider: str,
    tier: str | None,
    characters: int,
    already_billed_chars_this_month: int = 0,
    respect_free_tier: bool = True,
) -> tuple[int, bool]:
    """Return ``(cost_micro_usd, used_free_tier)``.

    Characters beyond the monthly free allowance are billed at the tier rate. The
    allowance is per (provider, tier) and consumed in order, so a request can straddle
    the boundary: part free, part billed.
    """
    spec = pricing.tier(provider, tier)
    if spec.usd_per_mchar <= 0:
        return 0, True

    free_remaining = 0
    if respect_free_tier and spec.free_chars_per_month > 0:
        free_remaining = max(0, spec.free_chars_per_month - already_billed_chars_this_month)

    if free_remaining >= characters:
        return 0, True

    billable = characters - free_remaining
    cost = round(billable * spec.micro_per_mchar / 1_000_000)
    return cost, free_remaining > 0


def month_bounds(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or dt.datetime.now(dt.UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + dt.timedelta(days=32)).replace(day=1)
    return start, nxt
