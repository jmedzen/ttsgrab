"""Long text: the parallel path must be faster *and* produce the same audio.

These use fake providers with artificial latency, so they assert the orchestration
property rather than the speed of any upstream service.
"""

from __future__ import annotations

import time

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
    started = time.perf_counter()
    sequential = await synthesize(ctx, req, bypass_cache=True)
    sequential_s = time.perf_counter() - started

    ctx.settings.longform.max_concurrency = 4
    started = time.perf_counter()
    parallel = await synthesize(ctx, req, bypass_cache=True)
    parallel_s = time.perf_counter() - started

    assert sequential.chunks == parallel.chunks > 1
    # Same characters, same provider set: batching must not change the result.
    assert len(parallel.result.audio) == len(sequential.result.audio)

    # Generous: the point is that it is not simply equal to the sequential time.
    assert parallel_s < sequential_s * 0.7, (
        f"parallel {parallel_s:.2f}s vs sequential {sequential_s:.2f}s — "
        "the chunks are not actually running concurrently"
    )


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
