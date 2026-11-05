"""Router / policy / fallback / circuit-breaker tests.

These cover the behaviour the acceptance checklist cares about: "kill the worker and the
request still succeeds, and the response says which provider actually served it".
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeBehaviour, FakeProvider, install
from ttsgrab.core.errors import AllProvidersFailed, BudgetExceeded
from ttsgrab.core.router import Orchestrator
from ttsgrab.core.types import CostClass, SynthRequest


def make(ctx, *providers) -> Orchestrator:
    install(ctx, *providers)
    return ctx.orchestrator


def req(**kw) -> SynthRequest:
    base = {"text": "hello world", "voice_id": "fake-voice"}
    base.update(kw)
    return SynthRequest(**base)


@pytest.mark.asyncio
async def test_explicit_provider_is_honoured(ctx) -> None:
    a = FakeProvider(ctx.settings, name="alpha")
    b = FakeProvider(ctx.settings, name="beta")
    a._voices[0] = a._voices[0].model_copy(update={"voice_id": "alpha-voice"})
    b._voices[0] = b._voices[0].model_copy(update={"voice_id": "beta-voice"})
    orch = make(ctx, a, b)

    result, meta = await orch.synthesize(req(provider="beta", voice_id="beta-voice"))
    assert result.provider == "beta"
    assert meta["used_provider"] == "beta"
    assert meta["fallback_used"] is False


@pytest.mark.asyncio
async def test_fallback_when_primary_fails(ctx) -> None:
    broken = FakeProvider(
        ctx.settings, name="broken", behaviour=FakeBehaviour(failures=99, error="down")
    )
    broken._voices[0] = broken._voices[0].model_copy(update={"voice_id": "shared"})
    healthy = FakeProvider(ctx.settings, name="healthy")
    healthy._voices[0] = healthy._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = ["broken", "healthy"]
    ctx.settings.router.max_fallbacks = 2
    orch = make(ctx, broken, healthy)

    result, meta = await orch.synthesize(req(provider="broken", voice_id="shared"))
    assert result.provider == "healthy"
    assert meta["fallback_used"] is True
    assert meta["requested_provider"] == "broken"
    assert [a["provider"] for a in meta["attempts"]] == ["broken", "healthy"]
    assert meta["attempts"][0]["ok"] is False


@pytest.mark.asyncio
async def test_no_fallback_when_disabled(ctx) -> None:
    broken = FakeProvider(ctx.settings, name="broken", behaviour=FakeBehaviour(failures=99))
    broken._voices[0] = broken._voices[0].model_copy(update={"voice_id": "shared"})
    healthy = FakeProvider(ctx.settings, name="healthy")
    healthy._voices[0] = healthy._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = ["broken", "healthy"]
    orch = make(ctx, broken, healthy)

    with pytest.raises(AllProvidersFailed):
        await orch.synthesize(req(provider="broken", voice_id="shared"), allow_fallback=False)


@pytest.mark.asyncio
async def test_everything_failing_raises_with_attempt_details(ctx) -> None:
    a = FakeProvider(ctx.settings, name="a", behaviour=FakeBehaviour(failures=99))
    b = FakeProvider(ctx.settings, name="b", behaviour=FakeBehaviour(failures=99))
    a._voices[0] = a._voices[0].model_copy(update={"voice_id": "shared"})
    b._voices[0] = b._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = ["a", "b"]
    orch = make(ctx, a, b)

    with pytest.raises(AllProvidersFailed) as exc:
        await orch.synthesize(req(provider="a", voice_id="shared"))
    assert len(exc.value.details["attempts"]) == 2
    assert exc.value.retryable is True


@pytest.mark.asyncio
async def test_circuit_opens_after_threshold(ctx) -> None:
    flaky = FakeProvider(ctx.settings, name="flaky", behaviour=FakeBehaviour(failures=99))
    flaky._voices[0] = flaky._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = ["flaky"]
    ctx.settings.router.circuit_breaker_threshold = 2
    orch = make(ctx, flaky)

    for _ in range(2):
        with pytest.raises(AllProvidersFailed):
            await orch.synthesize(req(provider="flaky", voice_id="shared"), allow_fallback=False)

    health = orch.health_of("flaky")
    assert health.breaker.failures >= 2
    assert orch._is_open("flaky") is True


@pytest.mark.asyncio
async def test_circuit_closes_on_success(ctx) -> None:
    provider = FakeProvider(ctx.settings, name="ok")
    provider._voices[0] = provider._voices[0].model_copy(update={"voice_id": "shared"})
    orch = make(ctx, provider)

    await orch.synthesize(req(provider="ok", voice_id="shared"))
    assert orch._is_open("ok") is False


@pytest.mark.asyncio
async def test_free_only_policy_excludes_paid_providers(ctx) -> None:
    free = FakeProvider(ctx.settings, name="free", cost_class=CostClass.FREE)
    paid = FakeProvider(ctx.settings, name="paid", cost_class=CostClass.PAID)
    for p in (free, paid):
        p._voices[0] = p._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = []
    orch = make(ctx, free, paid)

    result, _meta = await orch.synthesize(req(voice_id="shared"), policy="free_only")
    assert result.provider == "free"


@pytest.mark.asyncio
async def test_local_only_policy_excludes_cloud(ctx) -> None:
    cloud = FakeProvider(ctx.settings, name="cloud", kind="cloud")
    local = FakeProvider(ctx.settings, name="local", kind="local")
    for p in (cloud, local):
        p._voices[0] = p._voices[0].model_copy(update={"voice_id": "shared"})
    # Name the local provider one of the known-local names so the policy recognises it.
    local.name = "kokoro"
    local._voices[0] = local._voices[0].model_copy(update={"provider": "kokoro"})
    ctx.settings.router.fallback_chain = []
    orch = make(ctx, cloud, local)

    result, _meta = await orch.synthesize(req(voice_id="shared"), policy="local_only")
    assert result.provider == "kokoro"


@pytest.mark.asyncio
async def test_fastest_policy_picks_lowest_latency(ctx) -> None:
    slow = FakeProvider(ctx.settings, name="slow", behaviour=FakeBehaviour(latency_ms=200))
    fast = FakeProvider(ctx.settings, name="fast", behaviour=FakeBehaviour(latency_ms=5))
    for p in (slow, fast):
        p._voices[0] = p._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = []
    orch = make(ctx, slow, fast)

    # Prime the EWMA with one call each.
    await orch.synthesize(req(provider="slow", voice_id="shared"))
    await orch.synthesize(req(provider="fast", voice_id="shared"))
    result, _meta = await orch.synthesize(req(voice_id="shared"), policy="fastest")
    assert result.provider == "fast"


@pytest.mark.asyncio
async def test_voice_not_available_anywhere_raises(ctx) -> None:
    only = FakeProvider(ctx.settings, name="only")
    ctx.settings.router.fallback_chain = []
    orch = make(ctx, only)

    with pytest.raises(AllProvidersFailed):
        await orch.synthesize(req(voice_id="nobody-has-this"), policy="cheapest")


@pytest.mark.asyncio
async def test_disabled_provider_is_skipped(ctx) -> None:
    off = FakeProvider(ctx.settings, name="off", enabled=False)
    on = FakeProvider(ctx.settings, name="on")
    for p in (off, on):
        p._voices[0] = p._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = ["off", "on"]
    orch = make(ctx, off, on)

    result, _meta = await orch.synthesize(req(provider="off", voice_id="shared"))
    assert result.provider == "on"


@pytest.mark.asyncio
async def test_budget_exceeded_propagates_without_fallback(ctx) -> None:
    """A budget refusal must NOT be swallowed and retried on another cloud provider."""

    class BudgetProvider(FakeProvider):
        async def synthesize(self, request):
            raise BudgetExceeded("monthly cap hit")

    p = BudgetProvider(ctx.settings, name="greedy")
    p._voices[0] = p._voices[0].model_copy(update={"voice_id": "shared"})
    fallback = FakeProvider(ctx.settings, name="cheap")
    fallback._voices[0] = fallback._voices[0].model_copy(update={"voice_id": "shared"})
    ctx.settings.router.fallback_chain = ["cheap"]
    orch = make(ctx, p, fallback)

    with pytest.raises(BudgetExceeded):
        await orch.synthesize(req(provider="greedy", voice_id="shared"))
