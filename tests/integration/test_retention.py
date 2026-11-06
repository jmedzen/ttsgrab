"""Retention deletes user data, so the tests are about what it must NOT delete.

A pruning bug that removes too much is unrecoverable; one that removes too little is a disk
problem the operator can see. The tests are weighted accordingly.
"""

from __future__ import annotations

import datetime as dt
import uuid
from pathlib import Path

import pytest

from ttsgrab.core import retention
from ttsgrab.db.models import SynthRecord
from ttsgrab.db.session import session_scope


async def _add(created_at: dt.datetime, audio: Path | None = None) -> str:
    rid = uuid.uuid4().hex
    async with session_scope() as session:
        session.add(
            SynthRecord(
                id=rid,
                created_at=created_at,
                text="x" * 10,
                text_chars=10,
                provider="stub",
                voice_id="stub-tone",
                format="wav",
                duration_ms=100,
                latency_ms=10,
                cost_micro_usd=0,
                audio_path=str(audio) if audio else None,
            )
        )
    return rid


async def _ids() -> set[str]:
    from sqlalchemy import select

    async with session_scope() as session:
        return set((await session.execute(select(SynthRecord.id))).scalars().all())


@pytest.fixture
async def clean_history(app):
    """These tests own the table: leftovers from other tests would change the counts."""
    from sqlalchemy import delete

    async with session_scope() as session:
        await session.execute(delete(SynthRecord))
    yield
    async with session_scope() as session:
        await session.execute(delete(SynthRecord))


async def test_old_rows_are_removed(clean_history):
    # The keep window is a floor (`MIN_KEEP`), so age-based deletion only applies to rows
    # beyond it. Seed more than the floor or nothing is eligible.
    old = await _add(dt.datetime.now(dt.UTC) - dt.timedelta(days=400))
    for _ in range(retention.MIN_KEEP):
        await _add(dt.datetime.now(dt.UTC))
    new = await _add(dt.datetime.now(dt.UTC))

    await retention.prune_history(keep=retention.MIN_KEEP, max_age_days=90)

    left = await _ids()
    assert old not in left
    assert new in left


async def test_the_newest_rows_survive_regardless_of_age(clean_history):
    """A box that has been idle for a year must not be emptied on the first pass: the most
    recent synthesis is what the user is looking at."""
    ancient = [await _add(dt.datetime.now(dt.UTC) - dt.timedelta(days=900 + i)) for i in range(5)]

    await retention.prune_history(keep=3, max_age_days=1)

    left = await _ids()
    assert len(left & set(ancient)) >= 3, "keep-N must beat the age cutoff"


async def test_it_refuses_to_prune_below_the_floor(clean_history):
    """`keep=0` from a bad config must not delete everything."""
    ids = [await _add(dt.datetime.now(dt.UTC) - dt.timedelta(days=500)) for _ in range(30)]

    await retention.prune_history(keep=0, max_age_days=1)

    assert len(await _ids() & set(ids)) >= retention.MIN_KEEP


async def test_a_young_row_is_never_deleted(clean_history):
    fresh = await _add(dt.datetime.now(dt.UTC))

    await retention.prune_history(keep=retention.MIN_KEEP, max_age_days=90)

    assert fresh in await _ids()


async def test_audio_files_of_deleted_rows_go_with_them(clean_history, tmp_path):
    audio = tmp_path / "old.wav"
    audio.write_bytes(b"RIFF")
    await _add(dt.datetime.now(dt.UTC) - dt.timedelta(days=400), audio=audio)
    for _ in range(retention.MIN_KEEP):
        await _add(dt.datetime.now(dt.UTC))

    result = await retention.prune_history(keep=retention.MIN_KEEP, max_age_days=90)

    assert result["rows"] == 1
    assert not audio.exists(), "the file must not outlive its row"


async def test_a_missing_audio_file_does_not_block_the_row_deletion(clean_history, tmp_path):
    """Otherwise the row is retried forever and the prune reports progress it never makes."""
    await _add(dt.datetime.now(dt.UTC) - dt.timedelta(days=400), audio=tmp_path / "gone.wav")
    for _ in range(retention.MIN_KEEP):
        await _add(dt.datetime.now(dt.UTC))

    result = await retention.prune_history(keep=retention.MIN_KEEP, max_age_days=90)

    assert result["rows"] == 1


async def test_retention_is_off_by_default(app, settings):
    """An upgrade must not start deleting data nobody agreed to lose."""
    assert settings.retention.enabled is False
    assert await retention.run_once(settings) == {
        "rows": 0,
        "files": 0,
        "orphans": 0,
        "idempotency": 0,
    }


async def test_a_pruning_failure_never_escapes_the_loop(app, monkeypatch, settings):
    """The maintenance task crashing must not take the gateway with it."""

    async def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(retention, "run_once", boom)
    settings.retention.enabled = True

    task = __import__("asyncio").create_task(retention.background_loop(settings, interval_s=0))
    await __import__("asyncio").sleep(0.1)
    assert not task.done(), "the loop must survive a failed pass rather than exit"
    task.cancel()
    with pytest.raises(__import__("asyncio").CancelledError):
        await task
