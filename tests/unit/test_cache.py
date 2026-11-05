"""Cache behaviour: keying, TTL, LRU eviction."""

from __future__ import annotations

import asyncio

import pytest

from ttsgrab.core.cache import DiskCache
from ttsgrab.core.types import AudioFormat, SynthRequest


def test_cache_key_changes_with_every_audio_affecting_param() -> None:
    base = SynthRequest(text="hello", voice_id="v1")
    variants = [
        base.model_copy(update={"text": "hello!"}),
        base.model_copy(update={"voice_id": "v2"}),
        base.model_copy(update={"format": AudioFormat.WAV}),
        base.model_copy(update={"sample_rate": 16000}),
        base.model_copy(update={"speed": 1.1}),
        base.model_copy(update={"pitch": 1.0}),
        base.model_copy(update={"volume": 0.5}),
        base.model_copy(update={"style": "cheerful"}),
        base.model_copy(update={"prompt": "whisper"}),
        base.model_copy(update={"language": "zh-TW"}),
        base.model_copy(update={"reference_audio_id": "ref1"}),
        base.model_copy(update={"extra": {"k": 1}}),
    ]
    keys = {base.cache_key()}
    for variant in variants:
        keys.add(variant.cache_key())
    assert len(keys) == len(variants) + 1


def test_cache_key_is_stable_for_equivalent_requests() -> None:
    a = SynthRequest(text="你好", voice_id="v", speed=1.0000001)
    b = SynthRequest(text="你好", voice_id="v", speed=1.0)
    assert a.cache_key() == b.cache_key()


def test_cache_key_ignores_non_audio_params() -> None:
    a = SynthRequest(text="x", voice_id="v")
    b = SynthRequest(text="x", voice_id="v", strict=True)
    assert a.cache_key() == b.cache_key()


@pytest.mark.asyncio
async def test_put_get_roundtrip(tmp_path) -> None:
    cache = DiskCache(tmp_path, max_bytes=10_000, ttl_seconds=3600)
    await cache.put("abc123", b"audio-bytes", fmt=AudioFormat.MP3, provider="edge", voice_id="v1")
    entry = await cache.get("abc123")
    assert entry is not None
    assert await cache.read(entry) == b"audio-bytes"
    assert entry.meta["provider"] == "edge"
    assert cache.stats()["entries"] == 1


@pytest.mark.asyncio
async def test_miss_returns_none(tmp_path) -> None:
    cache = DiskCache(tmp_path, max_bytes=10_000, ttl_seconds=3600)
    assert await cache.get("nope") is None


@pytest.mark.asyncio
async def test_expired_entry_is_deleted(tmp_path) -> None:
    cache = DiskCache(tmp_path, max_bytes=10_000, ttl_seconds=1)
    await cache.put("k", b"x", fmt=AudioFormat.MP3, provider="p", voice_id="v")
    await asyncio.sleep(1.1)
    assert await cache.get("k") is None
    # The read path removes it rather than leaving a dead file around.
    assert cache.stats()["entries"] == 0


@pytest.mark.asyncio
async def test_lru_eviction_respects_budget(tmp_path) -> None:
    cache = DiskCache(tmp_path, max_bytes=3_000, ttl_seconds=3600)
    for i in range(10):
        await cache.put(f"key{i:04d}", b"x" * 1000, fmt=AudioFormat.MP3, provider="p", voice_id="v")
        await asyncio.sleep(0.01)
    assert cache.total_bytes() <= 3_000


@pytest.mark.asyncio
async def test_most_recently_used_survives_eviction(tmp_path) -> None:
    # Budget holds 3 clips; a 4th push must evict the least-recently-used one only.
    cache = DiskCache(tmp_path, max_bytes=3_500, ttl_seconds=3600)
    for i in range(3):
        await cache.put(f"k{i}", b"x" * 1000, fmt=AudioFormat.MP3, provider="p", voice_id="v")
        await asyncio.sleep(0.02)
    # Touching k0 makes it the newest, so k1 becomes the eviction candidate.
    assert await cache.get("k0") is not None
    await asyncio.sleep(0.02)
    await cache.put("k9", b"x" * 1000, fmt=AudioFormat.MP3, provider="p", voice_id="v")
    assert await cache.get("k0") is not None, "recently used entry was evicted"
    assert await cache.get("k1") is None, "least-recently-used entry should have been evicted"
    assert cache.total_bytes() <= 3_500


@pytest.mark.asyncio
async def test_disabled_cache_is_a_noop(tmp_path) -> None:
    cache = DiskCache(tmp_path, max_bytes=10_000, ttl_seconds=3600, enabled=False)
    await cache.put("k", b"x", fmt=AudioFormat.MP3, provider="p", voice_id="v")
    assert await cache.get("k") is None


@pytest.mark.asyncio
async def test_clear_removes_everything(tmp_path) -> None:
    cache = DiskCache(tmp_path, max_bytes=10_000, ttl_seconds=3600)
    await cache.put("a", b"x" * 10, fmt=AudioFormat.MP3, provider="p", voice_id="v")
    freed = await cache.clear()
    assert freed >= 10
    assert cache.stats()["entries"] == 0


@pytest.mark.asyncio
async def test_corrupt_metadata_is_a_miss_not_a_crash(tmp_path) -> None:
    cache = DiskCache(tmp_path, max_bytes=10_000, ttl_seconds=3600)
    await cache.put("k", b"x", fmt=AudioFormat.MP3, provider="p", voice_id="v")
    _blob, meta_path = cache._paths("k")
    meta_path.write_text("{not json", encoding="utf-8")
    assert await cache.get("k") is None
