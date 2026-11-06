"""Idempotent synthesis via a client-supplied `Idempotency-Key`.

Why this exists: an HTTP retry is routine — a dropped connection, a load balancer
timeout, an SDK's own retry — and without a key each attempt is a fresh synthesis. For a
free provider that wastes a daily allowance of ten. For a billed provider it is real money
charged twice for one sentence.

Semantics follow the widely-implemented Stripe/IETF convention:

- first request with a key runs normally and records the outcome;
- a repeat with the *same* key and the *same* body returns the stored result;
- a repeat with the same key but a *different* body is an error, not a silent replay,
  because returning audio for text the caller did not send is worse than failing.

Scope is (API key id, key). Two clients may coincidentally choose the same UUID.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete

from ttsgrab.db.models import IdempotencyRecord
from ttsgrab.db.session import session_scope

__all__ = ["IdempotencyConflict", "Replay", "begin", "finish", "purge", "request_fingerprint"]

# How long a key stays replayable. Long enough to cover any realistic retry window and a
# client restart; short enough that the table does not grow without bound.
RETENTION_HOURS = 24


class IdempotencyConflict(Exception):
    """The key was reused with a different request body."""


@dataclass(frozen=True)
class Replay:
    """A previous outcome for this key."""

    history_id: str | None


def request_fingerprint(payload: dict[str, Any]) -> str:
    """Stable hash of the request body.

    Sorted keys and a fixed separator: the caller may serialise their JSON in any order,
    and a byte-difference that is not a semantic difference must not look like one.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def begin(*, key: str, api_key_id: str | None, fingerprint: str) -> Replay | None:
    """Claim the key. Returns the previous outcome if this is a replay."""
    scoped = api_key_id or ""
    async with session_scope() as session:
        row = await session.get(IdempotencyRecord, (key, scoped))
        if row is None:
            session.add(
                IdempotencyRecord(
                    key=key,
                    api_key_id=scoped,
                    request_hash=fingerprint,
                    status="pending",
                )
            )
            await session.flush()
            return None
        if row.request_hash != fingerprint:
            raise IdempotencyConflict(
                "this Idempotency-Key was already used with a different request body"
            )
        if row.status == "done":
            return Replay(history_id=row.history_id)
        # A row left 'pending' means a previous attempt died mid-flight (process restart,
        # crash). Treating it as a replay would return nothing; re-running risks a double
        # charge. Re-running is the lesser evil and the row is updated in place.
        return None


async def finish(*, key: str, api_key_id: str | None, history_id: str | None) -> None:
    scoped = api_key_id or ""
    async with session_scope() as session:
        row = await session.get(IdempotencyRecord, (key, scoped))
        if row is None:
            return
        row.status = "done"
        row.history_id = history_id


async def purge(older_than_hours: int = RETENTION_HOURS) -> int:
    """Drop expired keys. Called opportunistically; never on the request path."""
    import datetime as dt

    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(hours=older_than_hours)
    async with session_scope() as session:
        result = await session.execute(
            delete(IdempotencyRecord).where(IdempotencyRecord.created_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)
