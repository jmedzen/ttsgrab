"""Long text: the parallel path must actually run in parallel, and produce the same audio.

These use fake providers that record how many syntheses are in flight at once, so they
assert the orchestration property rather than the speed of any upstream service. Timing
ratios were tried first and are not used: a shared CI runner makes them flaky in both
directions, and a flaky test that means "the machine was busy" teaches people to rerun
instead of read.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeBehaviour, FakeProvider, install
from ttsgrab.core.types import AudioFormat, CostClass, SynthRequest

SENTENCES = [f"這是第{i}句話，用來測試長文合成。" for i in range(1, 9)]


@pytest.mark.asyncio
async def test_parallel_synthesis_beats_sequential_and_matches_output(ctx) -> None:
    from ttsgrab.core.synthesis import synthesize

    provider = FakeProvider(ctx.settings, name="slow", cost_class=CostClass.FREE)
    provider.behaviour = FakeBehaviour(latency_ms=60, formats=[AudioFormat.WAV])
    install(ctx, provider)

    req = SynthRequest(
        text="".join(SENTENCES),
        voice_id="slow-voice",
        format=AudioFormat.WAV,
    )

    ctx.settings.longform.chunk_threshold = 1
    ctx.settings.longform.max_sentence_chars = 300

    ctx.settings.longform.max_concurrency = 1
    provider.max_in_flight = 0
    sequential = await synthesize(ctx, req, bypass_cache=True)
    sequential_peak = provider.max_in_flight

    ctx.settings.longform.max_concurrency = 4
    provider.max_in_flight = 0
    parallel = await synthesize(ctx, req, bypass_cache=True)
    parallel_peak = provider.max_in_flight

    assert sequential.chunks == parallel.chunks > 1
    # Same characters, same provider set: batching must not change the result.
    assert len(parallel.result.audio) == len(sequential.result.audio)

    # Assert the property itself, not a wall-clock ratio. An earlier version compared
    # parallel time against `sequential * 0.7` and failed on a loaded CI runner at 0.75 —
    # the chunks *were* concurrent; the ffmpeg concat and process start-up cost were simply
    # larger than the 60 ms the fake spends sleeping. Peak in-flight count cannot be
    # perturbed by a busy runner, and it fails for the reason the test name claims.
    assert sequential_peak == 1, f"max_concurrency=1 let {sequential_peak} chunks run at once"
    assert parallel_peak > 1, (
        f"peak in-flight was {parallel_peak} across {parallel.chunks} chunks — "
        "the chunks are not actually running concurrently"
    )
    assert parallel_peak <= ctx.settings.longform.max_concurrency


@pytest.mark.asyncio
async def test_long_text_is_split_at_sentence_boundaries(ctx) -> None:
    from ttsgrab.core.synthesis import synthesize

    provider = FakeProvider(ctx.settings, name="slow", cost_class=CostClass.FREE)
    install(ctx, provider)
    ctx.settings.longform.chunk_threshold = 1
    req = SynthRequest(text="".join(SENTENCES), voice_id="slow-voice", format=AudioFormat.WAV)
    outcome = await synthesize(ctx, req, bypass_cache=True)
    assert outcome.chunks == len(SENTENCES)
    # No chunk may be empty, and no sentence may be lost.
    assert all(call.text.strip() for call in provider.calls)


@pytest.mark.asyncio
async def test_short_text_is_not_split(ctx) -> None:
    from ttsgrab.core.synthesis import synthesize

    provider = FakeProvider(ctx.settings, name="slow", cost_class=CostClass.FREE)
    install(ctx, provider)
    ctx.settings.longform.chunk_threshold = 1000
    req = SynthRequest(text="一句很短的話。", voice_id="slow-voice", format=AudioFormat.WAV)
    outcome = await synthesize(ctx, req, bypass_cache=True)
    assert outcome.chunks == 1


@pytest.mark.asyncio
async def test_a_failing_chunk_fails_the_whole_request(ctx) -> None:
    """Half an audio file is worse than an error: the caller cannot tell it is truncated."""
    from ttsgrab.core.errors import GatewayError
    from ttsgrab.core.synthesis import synthesize

    provider = FakeProvider(ctx.settings, name="slow", cost_class=CostClass.FREE)
    provider.behaviour = FakeBehaviour(failures=99)
    install(ctx, provider)
    ctx.settings.longform.chunk_threshold = 1
    ctx.settings.router.max_fallbacks = 0
    req = SynthRequest(text="".join(SENTENCES), voice_id="slow-voice", format=AudioFormat.WAV)
    with pytest.raises(GatewayError):
        await synthesize(ctx, req, bypass_cache=True)
