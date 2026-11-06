"""Bounded growth for history, audio and idempotency rows.

Everything this gateway writes used to be permanent: every synthesis left a row, every row
could leave an audio file, and `idempotency.purge()` existed but was never called. On a box
that runs for months that is a slow disk leak with no upper bound and no way to notice until
the volume is full.

Two limits, applied together, because either alone is wrong:

* **Age** — a two-year-old transcript is not history, it is sediment.
* **Count** — a chatty day can write more rows than a whole quiet year, so age alone lets a
  burst blow the disk before anything expires.

The newest rows are always kept regardless of age. Deleting the thing the user just made
would be a worse bug than the leak.

Note this runs on a timer, not per request: doing it inline would put a `DELETE` and a
directory scan in the synthesis path, where it would show up as latency.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from ttsgrab.core.idempotency import purge as purge_idempotency
from ttsgrab.db.models import SynthRecord
from ttsgrab.db.session import session_scope

if TYPE_CHECKING:
    from ttsgrab.settings import Settings

log = logging.getLogger(__name__)

# Never prune below this, even if the operator sets something smaller: deleting *all*
# history because a config value was mistyped is not recoverable.
MIN_KEEP = 20


async def prune_history(*, keep: int, max_age_days: int) -> dict[str, int]:
    """Delete old history rows and their audio files. Returns what was removed."""
    keep = max(keep, MIN_KEEP)
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=max_age_days)

    removed_rows = 0
    removed_files = 0
    async with session_scope() as session:
        # Newest first, skip the ones we are keeping, then delete the rest that are also
        # older than the cutoff. Doing it in two steps rather than one `OFFSET` keeps the
        # query readable and the intent obvious.
        survivors = (
            (
                await session.execute(
                    select(SynthRecord.id).order_by(SynthRecord.created_at.desc()).limit(keep)
                )
            )
            .scalars()
            .all()
        )
        keep_ids = set(survivors)

        doomed = (
            (await session.execute(select(SynthRecord).where(SynthRecord.created_at < cutoff)))
            .scalars()
            .all()
        )
        for row in doomed:
            if row.id in keep_ids:
                continue
            if row.audio_path:
                # Best effort: a missing file must not stop the row from being deleted, or
                # the row would be retried forever. Offloaded because this is a syscall.
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(Path(row.audio_path).unlink, True)
                    removed_files += 1
            await session.execute(delete(SynthRecord).where(SynthRecord.id == row.id))
            removed_rows += 1

    return {"rows": removed_rows, "files": removed_files}


async def prune_orphan_audio(settings: Settings, *, max_age_days: int) -> int:
    """Remove audio files with no history row.

    These appear when a row was deleted while its file could not be (permissions, a race),
    and they are invisible: no row points at them, so nothing would ever clean them up.
    """
    root = Path(settings.data_path) / "audio"
    if not root.is_dir():
        return 0

    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=max_age_days)).timestamp()
    async with session_scope() as session:
        known = set((await session.execute(select(SynthRecord.audio_path))).scalars().all())

    removed = 0
    for path in await asyncio.to_thread(lambda: list(root.iterdir())):
        if str(path) in known:
            continue
        try:
            stat = await asyncio.to_thread(path.stat)
            if stat.st_mtime > cutoff:
                # Young orphans may be a synthesis still being persisted; leave them.
                continue
            await asyncio.to_thread(path.unlink)
            removed += 1
        except OSError:
            continue
    return removed


async def run_once(settings: Settings) -> dict[str, int]:
    """Apply every retention rule once. Safe to call repeatedly."""
    retention = settings.retention
    if not retention.enabled:
        return {"rows": 0, "files": 0, "orphans": 0, "idempotency": 0}

    pruned = await prune_history(keep=retention.keep_records, max_age_days=retention.max_age_days)
    orphans = await prune_orphan_audio(settings, max_age_days=1)
    idem = await purge_idempotency(retention.idempotency_hours)

    total = {**pruned, "orphans": orphans, "idempotency": idem}
    if any(total.values()):
        log.info("retention: %s", total)
    return total


async def background_loop(settings: Settings, interval_s: float = 3600.0) -> None:
    """Hourly maintenance task. Cancelled on shutdown."""
    while True:
        try:
            await asyncio.sleep(interval_s)
            await run_once(settings)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A maintenance failure must never take the gateway down; it is not serving
            # requests and the next tick will try again.
            log.exception("retention pass failed")
