"""Disk cache for synthesised audio.

Key = hash of (provider, voice, every audio-affecting param, text). Sharded two levels deep
so a directory with a million files stays usable. Eviction is LRU by mtime under a byte
budget; TTL is enforced on read.

The cache is deliberately *not* a Redis dependency: a single-node gateway with a few
thousand clips does not need one, and SQLite-free disk storage keeps Docker simple.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ttsgrab.core.types import AudioFormat


@dataclass
class CacheEntry:
    path: Path
    meta: dict[str, Any]

    @property
    def format(self) -> AudioFormat:
        return AudioFormat(self.meta["format"])

    @property
    def provider(self) -> str:
        return self.meta["provider"]

    @property
    def voice_id(self) -> str:
        return self.meta["voice_id"]


class DiskCache:
    def __init__(self, root: Path, *, max_bytes: int, ttl_seconds: int, enabled: bool = True):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.ttl_seconds = ttl_seconds
        self.enabled = enabled
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()

    # -- paths ---------------------------------------------------------------------------
    def _paths(self, key: str) -> tuple[Path, Path]:
        shard = self.root / key[:2] / key[2:4]
        return shard / f"{key}.bin", shard / f"{key}.json"

    # -- read / write --------------------------------------------------------------------
    async def get(self, key: str) -> CacheEntry | None:
        if not self.enabled:
            return None
        blob_path, meta_path = self._paths(key)
        if not blob_path.is_file() or not meta_path.is_file():
            return None
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if self.ttl_seconds and time.time() - meta.get("created_at", 0) > self.ttl_seconds:
            await self.delete(key)
            return None
        # Touch for LRU.
        now = time.time()
        with contextlib.suppress(OSError):
            os.utime(blob_path, (now, now))
        return CacheEntry(path=blob_path, meta=meta)

    async def put(
        self,
        key: str,
        audio: bytes,
        *,
        fmt: AudioFormat,
        provider: str,
        voice_id: str,
        meta: dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled:
            return
        blob_path, meta_path = self._paths(key)
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "key": key,
            "format": fmt.value,
            "provider": provider,
            "voice_id": voice_id,
            "bytes": len(audio),
            "created_at": time.time(),
            **(meta or {}),
        }
        await asyncio.to_thread(blob_path.write_bytes, audio)
        await asyncio.to_thread(meta_path.write_text, json.dumps(record), "utf-8")
        if audio and self.total_bytes() > self.max_bytes:
            await self.evict()

    async def read(self, entry: CacheEntry) -> bytes:
        return await asyncio.to_thread(entry.path.read_bytes)

    async def delete(self, key: str) -> None:
        for p in self._paths(key):
            await asyncio.to_thread(p.unlink, True)

    # -- maintenance ----------------------------------------------------------------------
    def total_bytes(self) -> int:
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                if name.endswith(".bin"):
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(os.path.join(dirpath, name))
        return total

    async def evict(self) -> int:
        """Drop least-recently-used entries until under budget. Returns bytes freed."""
        async with self._lock:
            entries: list[tuple[float, int, Path]] = []
            for dirpath, _dirnames, filenames in os.walk(self.root):
                for name in filenames:
                    if not name.endswith(".bin"):
                        continue
                    path = Path(dirpath) / name
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    entries.append((st.st_mtime, st.st_size, path))
            entries.sort()
            total = sum(size for _m, size, _p in entries)
            freed = 0
            for _mtime, size, path in entries:
                if total <= self.max_bytes:
                    break
                try:
                    path.unlink()
                    path.with_suffix(".json").unlink(missing_ok=True)
                except OSError:
                    continue
                total -= size
                freed += size
            return freed

    async def clear(self) -> int:
        freed = self.total_bytes()
        import shutil

        await asyncio.to_thread(shutil.rmtree, self.root, True)
        self.root.mkdir(parents=True, exist_ok=True)
        return freed

    def stats(self) -> dict[str, Any]:
        count = 0
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for name in filenames:
                if name.endswith(".bin"):
                    count += 1
                    with contextlib.suppress(OSError):
                        total += os.path.getsize(os.path.join(dirpath, name))
        return {
            "enabled": self.enabled,
            "entries": count,
            "bytes": total,
            "max_bytes": self.max_bytes,
            "ttl_seconds": self.ttl_seconds,
            "used_pct": round(total / self.max_bytes * 100, 1) if self.max_bytes else 0,
        }
