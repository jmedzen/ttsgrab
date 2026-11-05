"""Worker health monitor: periodic checks, EWMA latency, automatic eviction.

Health state lives in the DB so it survives restarts and the UI can show it without a
second source of truth. The router consults the circuit breaker, not this table, for
per-request decisions — but the monitor is what populates it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from sqlalchemy import select

from ttsgrab.core.registry import ProviderRegistry
from ttsgrab.db.models import WorkerNode
from ttsgrab.db.session import session_scope
from ttsgrab.workers.client import WorkerProvider

logger = logging.getLogger(__name__)


class WorkerMonitor:
    def __init__(self, ctx_deps) -> None:
        self.settings = ctx_deps.settings
        self.registry: ProviderRegistry = ctx_deps.registry
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    async def load_workers(self) -> int:
        """Instantiate WorkerProviders for every enabled node in the DB."""
        async with session_scope() as session:
            rows = (await session.execute(select(WorkerNode))).scalars().all()
        count = 0
        for node in rows:
            provider = WorkerProvider(node, self.settings)
            self.registry.add_instance(provider)
            count += 1
            logger.info("worker registered: %s -> %s (%s)", node.name, node.base_url, node.kind)
        return count

    async def check_one(self, node_id: str) -> WorkerNode | None:
        async with session_scope() as session:
            node = await session.get(WorkerNode, node_id)
            if node is None:
                return None
            provider = self.registry.get(node.name)
            started = time.perf_counter()
            try:
                if provider is None:
                    raise RuntimeError("worker not instantiated")
                await provider.health()
                voices = await provider.list_voices()
                provider._voices = voices
                node.healthy = True
                node.consecutive_failures = 0
                node.consecutive_successes += 1
                node.last_error = None
                latency = (time.perf_counter() - started) * 1000
                node.latency_ms_ewma = (
                    latency
                    if node.latency_ms_ewma is None
                    else 0.4 * latency + 0.6 * node.latency_ms_ewma
                )
            except Exception as exc:
                node.healthy = False
                node.consecutive_failures += 1
                node.consecutive_successes = 0
                node.last_error = f"{type(exc).__name__}: {exc}"[:500]
                if node.consecutive_failures >= self.settings.workers.unhealthy_threshold:
                    logger.warning(
                        "worker %s evicted after %d failures: %s",
                        node.name,
                        node.consecutive_failures,
                        node.last_error,
                    )
            node.last_checked_at = __import__("datetime").datetime.now(__import__("datetime").UTC)
            await session.flush()
            await session.refresh(node)
            return node

    async def check_all(self) -> None:
        async with session_scope() as session:
            ids = [r.id for r in (await session.execute(select(WorkerNode))).scalars().all()]
        for node_id in ids:
            try:
                await self.check_one(node_id)
            except Exception:
                logger.exception("health check failed for %s", node_id)

    async def _loop(self) -> None:
        interval = self.settings.workers.health_interval_s
        while not self._stop.is_set():
            try:
                await self.check_all()
            except Exception:
                logger.exception("worker monitor iteration failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    def start(self) -> None:
        if self._task is None:
            self._stop.clear()
            self._task = asyncio.create_task(self._loop(), name="worker-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.CancelledError):
                # Cancellation is a request, not a guarantee: without this await the loop
                # may still be running against a closed event loop, which surfaces later as
                # "Event loop is closed" in an unrelated test or on shutdown.
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._task
            self._task = None
