"""Routing, fallback and circuit breaking.

The router is the only place that decides which provider serves a request. Everything
else (API layer, jobs, MCP) calls :meth:`Orchestrator.synthesize`.

Policies
--------
``explicit``     Use exactly the requested provider; still fallback if it fails (unless
                 ``allow_fallback=False``).
``cheapest``     Lowest estimated cost first, then lowest measured latency.
``fastest``      Lowest EWMA latency among healthy providers.
``best_quality`` Highest configured quality score.
``local_only``   Only self-hosted providers/workers (no network egress).
``free_only``    Only providers whose audio never costs money (``cost_class == free``).

Circuit breaking
----------------
Failures are counted per provider in memory. After ``threshold`` consecutive failures a
provider is skipped for ``cooldown_s``. This is what makes "shut down the worker and the
request still succeeds" work in practice, rather than waiting on a connect timeout.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from ttsgrab.core.errors import (
    AllProvidersFailed,
    BudgetExceeded,
    GatewayError,
    ProviderError,
    ProviderUnavailable,
    UnsupportedParam,
)
from ttsgrab.core.registry import ProviderRegistry
from ttsgrab.core.types import CostClass, SynthRequest, SynthResult
from ttsgrab.providers.base import ParamSupportError, TTSProvider

Policy = Literal["cheapest", "fastest", "best_quality", "local_only", "free_only", "explicit"]

LOCAL_PROVIDERS = {"kokoro", "gpt_sovits", "cosyvoice", "piper"}


@dataclass
class Breaker:
    failures: int = 0
    opened_at: float | None = None
    successes: int = 0

    def record_success(self) -> None:
        self.failures = 0
        self.successes += 1
        self.opened_at = None

    def record_failure(self, threshold: int) -> None:
        self.failures += 1
        self.successes = 0
        if self.failures >= threshold and self.opened_at is None:
            self.opened_at = time.time()

    def is_open(self, cooldown_s: float) -> bool:
        if self.opened_at is None:
            return False
        if time.time() - self.opened_at >= cooldown_s:
            # Half-open: allow one probe through.
            self.opened_at = None
            self.failures = max(0, self.failures - 1)
            return False
        return True


@dataclass
class ProviderHealth:
    latencies: list[float] = field(default_factory=list)  # ms, most recent last
    breaker: Breaker = field(default_factory=Breaker)

    def record(self, latency_ms: int, *, window: int = 10) -> None:
        self.latencies.append(float(latency_ms))
        if len(self.latencies) > window:
            self.latencies.pop(0)

    @property
    def ewma_ms(self) -> float | None:
        if not self.latencies:
            return None
        alpha = 0.4
        value = self.latencies[0]
        for sample in self.latencies[1:]:
            value = alpha * sample + (1 - alpha) * value
        return value

    @property
    def p50_ms(self) -> int | None:
        if not self.latencies:
            return None
        ordered = sorted(self.latencies)
        return int(ordered[len(ordered) // 2])


class Orchestrator:
    def __init__(
        self,
        registry: ProviderRegistry,
        settings,
        *,
        usage=None,
        cache=None,
        pricing=None,
        db_factory=None,
    ) -> None:
        self.registry = registry
        self.settings = settings
        self.usage = usage
        self.cache = cache
        self.pricing = pricing
        self._db_factory = db_factory
        self.health: dict[str, ProviderHealth] = {}
        self._lock = asyncio.Lock()

    def health_of(self, provider: str) -> ProviderHealth:
        return self.health.setdefault(provider, ProviderHealth())

    # -- candidate selection ---------------------------------------------------------------
    def _tier_for(self, provider: TTSProvider, req: SynthRequest) -> str | None:
        for voice in provider.cached_voices:
            if voice.voice_id == req.voice_id:
                return voice.tier
        return None

    def _estimate_cost(self, provider: TTSProvider, req: SynthRequest) -> float:
        tier = self._tier_for(provider, req)
        if self.pricing is None:
            return 0.0
        spec = self.pricing.tier(provider.name, tier)
        return spec.usd_per_mchar * len(req.text) / 1_000_000

    def candidates(self, req: SynthRequest, policy: Policy) -> list[str]:
        """Ordered provider names to try."""
        enabled = {p.name: p for p in self.registry.enabled()}
        if not enabled:
            return []

        if req.provider:
            primary = [req.provider]
        elif policy == "explicit":
            primary = []
        else:
            primary = []

        if not primary:
            ranked = list(enabled.values())
            if policy == "local_only":
                ranked = [p for p in ranked if p.name in LOCAL_PROVIDERS or p.kind == "worker"]
            elif policy == "free_only":
                ranked = [p for p in ranked if p.cost_class == CostClass.FREE]
            elif policy == "cheapest":
                ranked.sort(key=lambda p: (self._estimate_cost(p, req), self._ewma(p)))
            elif policy == "fastest":
                ranked.sort(key=lambda p: self._ewma(p))
            elif policy == "best_quality":
                scores = self.settings.router.quality_scores
                ranked.sort(key=lambda p: -scores.get(p.name, 0.5))
            primary = [p.name for p in ranked if self._voice_ok(p, req)]

        # Fallback chain from config, filtered to enabled+healthy+voice-compatible.
        fallbacks: list[str] = []
        for name in self.settings.router.fallback_chain:
            if name in primary or name not in enabled:
                continue
            if policy == "local_only" and not (
                name in LOCAL_PROVIDERS or enabled[name].kind == "worker"
            ):
                continue
            if policy == "free_only" and enabled[name].cost_class != CostClass.FREE:
                continue
            if not self._voice_ok(enabled[name], req):
                continue
            fallbacks.append(name)

        ordered = [*primary, *fallbacks]
        # Apply the circuit breaker as a *preference*, not a hard filter, so that a single
        # flaky provider cannot make the whole gateway unusable.
        healthy = [n for n in ordered if not self._is_open(n)]
        open_ones = [n for n in ordered if self._is_open(n)]
        return [*healthy, *open_ones]

    def _voice_ok(self, provider: TTSProvider, req: SynthRequest) -> bool:
        if provider.name == req.provider:
            return True
        voices = provider.cached_voices
        if not voices:
            return False
        return any(v.voice_id == req.voice_id for v in voices)

    def _is_open(self, name: str) -> bool:
        return self.health_of(name).breaker.is_open(self.settings.router.circuit_breaker_cooldown_s)

    def _ewma(self, provider: TTSProvider) -> float:
        value = self.health_of(provider.name).ewma_ms
        return value if value is not None else 1_000.0

    # -- execution ---------------------------------------------------------------------------
    async def synthesize(
        self,
        req: SynthRequest,
        *,
        policy: Policy | None = None,
        allow_fallback: bool = True,
        api_key_id: str | None = None,
        db_session=None,
    ) -> tuple[SynthResult, dict[str, Any]]:
        policy = policy or self.settings.router.default_policy
        order = self.candidates(req, policy)
        if req.provider and req.provider not in order:
            order.insert(0, req.provider)
        if not order:
            raise AllProvidersFailed(
                "no provider can serve this voice",
                details={"voice_id": req.voice_id, "policy": policy},
            )

        order = order[:1] if not allow_fallback else order[: 1 + self.settings.router.max_fallbacks]

        attempts: list[dict[str, Any]] = []
        for name in order:
            provider = self.registry.get(name)
            if provider is None or not provider.enabled:
                attempts.append({"provider": name, "ok": False, "error": "disabled"})
                continue
            attempt_req = req.model_copy(update={"provider": name})

            # Re-check the budget for the provider we are about to use, not just the one
            # that was requested. Falling back from a free provider to a paid one must not
            # be a way around the cap.
            if self.usage is not None and self._db_factory is not None:
                tier = next(
                    (v.tier for v in provider.cached_voices if v.voice_id == req.voice_id), None
                )
                async with self._db_factory() as budget_session:
                    await self.usage.check_budget(
                        budget_session,
                        provider=name,
                        tier=tier,
                        characters=len(req.text or ""),
                    )

            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    provider.synthesize(attempt_req),
                    timeout=self.settings.router.request_timeout_s,
                )
            except ParamSupportError as exc:
                attempts.append({"provider": name, "ok": False, "error": str(exc)})
                self.health_of(name).breaker.record_failure(
                    self.settings.router.circuit_breaker_threshold
                )
                if req.strict:
                    raise UnsupportedParam(
                        f"{name}: {exc}",
                        param=", ".join(exc.params),
                        details={
                            "provider": name,
                            "params": list(exc.params),
                            "unsupported": list(exc.params),
                        },
                    ) from exc
                continue
            except BudgetExceeded:
                raise
            except ProviderUnavailable as exc:
                attempts.append({"provider": name, "ok": False, "error": str(exc)})
                self.health_of(name).breaker.record_failure(
                    self.settings.router.circuit_breaker_threshold
                )
                continue
            except (ProviderError, TimeoutError) as exc:
                attempts.append({"provider": name, "ok": False, "error": str(exc)})
                self.health_of(name).breaker.record_failure(
                    self.settings.router.circuit_breaker_threshold
                )
                continue
            except GatewayError:
                raise
            except Exception as exc:
                attempts.append({"provider": name, "ok": False, "error": repr(exc)})
                self.health_of(name).breaker.record_failure(
                    self.settings.router.circuit_breaker_threshold
                )
                continue

            elapsed = int((time.perf_counter() - started) * 1000)
            health = self.health_of(name)
            health.record(result.latency_ms or elapsed)
            health.breaker.record_success()
            attempts.append({"provider": name, "ok": True, "latency_ms": elapsed})

            meta = {
                "policy": policy,
                "requested_provider": req.provider,
                "used_provider": name,
                "fallback_used": name != (req.provider or order[0]),
                "attempts": attempts,
                "tier": result.extra.get("tier") or self._tier_for(provider, req),
            }
            return result, meta

        raise AllProvidersFailed(
            "every candidate provider failed",
            details={"attempts": attempts, "voice_id": req.voice_id, "policy": policy},
            suggested_fallback=["edge", "kokoro"],
        )

    def snapshot(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, health in self.health.items():
            out[name] = {
                "ewma_ms": round(health.ewma_ms, 1) if health.ewma_ms is not None else None,
                "p50_ms": health.p50_ms,
                "samples": len(health.latencies),
                "failures": health.breaker.failures,
                "circuit_open": self._is_open(name),
            }
        return out
