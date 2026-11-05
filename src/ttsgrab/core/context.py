"""Runtime container wiring all singletons together.

Built once at app startup and stored on ``app.state.ctx``. Kept free of FastAPI imports so
it can be constructed in tests without an app.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ttsgrab.core.cache import DiskCache
from ttsgrab.core.pricing import PricingTable
from ttsgrab.core.registry import ProviderRegistry
from ttsgrab.core.router import Orchestrator
from ttsgrab.core.textsplit import PronunciationDictionary
from ttsgrab.core.usage import UsageLedger
from ttsgrab.db.session import create_all, init_engine, session_factory
from ttsgrab.settings import Settings

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    registry: ProviderRegistry
    pricing: PricingTable
    cache: DiskCache
    usage: UsageLedger
    orchestrator: Orchestrator
    dictionary: PronunciationDictionary
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def db_factory(self):
        return session_factory()


def _find_pricing_file(config_dir: Path | None) -> Path | None:
    """Locate ``pricing.yaml`` without depending on the process working directory.

    Order: explicit ``config_dir``, ``$TTSGRAB_CONFIG_DIR``, the config file's own directory,
    the CWD, then the packaged copy. A container starts in ``/`` and a systemd unit starts in
    ``/``, so a CWD-only lookup would silently fall back to built-in prices.
    """
    candidates: list[Path] = []
    if config_dir is not None:
        candidates.append(config_dir / "pricing.yaml")
    env_dir = os.environ.get("TTSGRAB_CONFIG_DIR")
    if env_dir:
        candidates.append(Path(env_dir) / "pricing.yaml")
    cfg = os.environ.get("TTSGRAB_CONFIG")
    if cfg:
        candidates.append(Path(cfg).parent / "pricing.yaml")
    candidates.append(Path.cwd() / "pricing.yaml")
    package_root = Path(__file__).resolve().parent.parent
    # Shipped inside the wheel, so an installed package still has a copy to read.
    candidates.append(package_root / "pricing.yaml")
    # A source checkout: the repo root, one level above the package.
    candidates.append(package_root.parent / "pricing.yaml")
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


async def build_context(settings: Settings, *, config_dir: Path | None = None) -> AppContext:
    pricing_path = _find_pricing_file(config_dir)
    raw_overrides = getattr(settings, "_pricing_overrides", None)
    pricing = PricingTable.load(pricing_path)
    if raw_overrides:
        pricing = pricing.with_overrides(raw_overrides)

    init_engine(settings.database_url)
    await create_all()

    registry = ProviderRegistry(settings)
    registry.build()
    await registry.startup_all()

    cache = DiskCache(
        Path(settings.cache.dir),
        max_bytes=settings.cache.max_bytes,
        ttl_seconds=settings.cache.ttl_seconds,
        enabled=settings.cache.enabled,
    )
    usage = UsageLedger(pricing, settings)
    orchestrator = Orchestrator(
        registry,
        settings,
        usage=usage,
        cache=cache,
        pricing=pricing,
        db_factory=session_factory(),
    )

    dictionary = PronunciationDictionary(getattr(settings, "_pronunciation_overrides", None))

    ctx = AppContext(
        settings=settings,
        registry=registry,
        pricing=pricing,
        cache=cache,
        usage=usage,
        orchestrator=orchestrator,
        dictionary=dictionary,
    )
    logger.info(
        "context ready: %d providers enabled, %d voices, pricing v%s",
        len(registry.enabled()),
        len(registry.catalogue()),
        pricing.version,
    )
    return ctx
