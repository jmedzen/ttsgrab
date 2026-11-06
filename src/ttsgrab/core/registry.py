"""Provider registry.

Providers self-register through :func:`register`. The registry instantiates them from
settings at startup, tolerating failures: a provider that cannot initialise is marked
disabled with a reason, and the service keeps running.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Iterable

from ttsgrab.core.types import Voice
from ttsgrab.providers.base import TTSProvider

logger = logging.getLogger(__name__)

# Modules imported at startup so their @register decorators run. Order == UI display order.
BUILTIN_MODULES: tuple[str, ...] = (
    "ttsgrab.providers.edge",
    "ttsgrab.providers.gtts",
    "ttsgrab.providers.google_cloud",
    "ttsgrab.providers.google_ai_studio",
    "ttsgrab.providers.azure",
)

_REGISTRY: dict[str, type[TTSProvider]] = {}


def register(cls: type[TTSProvider]) -> type[TTSProvider]:
    if not getattr(cls, "name", None):
        raise ValueError(f"{cls.__name__} must define a class-level `name`")
    existing = _REGISTRY.get(cls.name)
    if existing is not None and existing is not cls:
        raise ValueError(f"duplicate provider name {cls.name!r}")
    _REGISTRY[cls.name] = cls
    return cls


def registered_classes() -> dict[str, type[TTSProvider]]:
    return dict(_REGISTRY)


def load_builtin_providers() -> None:
    for mod in BUILTIN_MODULES:
        try:
            importlib.import_module(mod)
        except ModuleNotFoundError as exc:
            logger.warning("provider module %s unavailable: %s", mod, exc)


class ProviderRegistry:
    """Live instances keyed by name, with lazy, failure-isolated construction."""

    def __init__(self, settings) -> None:
        self._settings = settings
        self._instances: dict[str, TTSProvider] = {}
        self._errors: dict[str, str] = {}

    def build(self, names: Iterable[str] | None = None) -> None:
        load_builtin_providers()
        for name, cls in _REGISTRY.items():
            if names is not None and name not in names:
                continue
            try:
                self._instances[name] = cls(self._settings)
            except Exception as exc:
                logger.exception("failed to construct provider %s", name)
                self._errors[name] = str(exc)

    def add_instance(self, provider: TTSProvider) -> None:
        self._instances[provider.name] = provider

    def get(self, name: str) -> TTSProvider | None:
        return self._instances.get(name)

    def require(self, name: str) -> TTSProvider:
        provider = self._instances.get(name)
        if provider is None:
            from ttsgrab.core.errors import NotFound

            raise NotFound(f"unknown provider {name!r}")
        return provider

    def all(self) -> list[TTSProvider]:
        return list(self._instances.values())

    def enabled(self) -> list[TTSProvider]:
        return [p for p in self._instances.values() if p.enabled]

    def errors(self) -> dict[str, str]:
        return dict(self._errors)

    def catalogue(self) -> list[Voice]:
        out: list[Voice] = []
        for p in self.enabled():
            out.extend(p.cached_voices)
        return out

    def find_voice(self, provider: str, voice_id: str) -> Voice | None:
        p = self._instances.get(provider)
        if p is None:
            return None
        for v in p.cached_voices:
            if v.voice_id == voice_id:
                return v
        return None

    def voice_exists_anywhere(self, voice_id: str) -> str | None:
        for p in self._instances.values():
            if any(v.voice_id == voice_id for v in p.cached_voices):
                return p.name
        return None

    async def startup_all(self) -> None:
        import asyncio

        async def _boot(p: TTSProvider) -> None:
            try:
                await p.startup()
                if p.enabled:
                    await p.refresh_voices()
            except Exception as exc:
                logger.warning("provider %s failed to start: %s", p.name, exc)
                self._errors[p.name] = str(exc)

        await asyncio.gather(*(_boot(p) for p in self._instances.values()))

    async def shutdown_all(self) -> None:
        import asyncio

        async def _stop(p: TTSProvider) -> None:
            try:
                await p.shutdown()
            except Exception:
                logger.exception("provider %s failed to shut down", p.name)

        await asyncio.gather(*(_stop(p) for p in self._instances.values()))
