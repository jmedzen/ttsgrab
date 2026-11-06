"""Favourite voices, stored server-side.

Why a table and not localStorage: this gateway is meant to be run by one person or a small
team against a single instance. Favourites kept in the browser are tied to that browser —
you star a voice on your laptop and it is missing on your phone, which is the opposite of
what "favourite" implies. The catalogue is shared, so the shortlist of it should be too.

The list is small and contains nothing sensitive; it is readable by anyone who can already
read the catalogue. Mutations are admin-only, matching how providers and keys are managed,
because a shared instance should not let any passer-by reorder the team's shortlist.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ttsgrab.api.deps import AdminDep, Ctx, PrincipalDep
from ttsgrab.core.errors import InvalidRequest
from ttsgrab.db.models import FavouriteVoice
from ttsgrab.db.session import session_scope

router = APIRouter(tags=["favourites"])

# A shortlist longer than this stops being a shortcut. Matches the frontend's cap so the UI
# and the API agree on what "full" means.
MAX_FAVOURITES = 24


class FavouriteIn(BaseModel):
    voice_id: str = Field(min_length=1, max_length=128)
    provider: str | None = Field(default=None, max_length=64)
    name: str | None = Field(default=None, max_length=256)


def _out(row: FavouriteVoice) -> dict[str, Any]:
    return {
        "voice_id": row.voice_id,
        "provider": row.provider,
        "name": row.name,
        "position": row.position,
        "created_at": row.created_at.isoformat(),
    }


@router.get("/v1/favourites", summary="List favourite voices in display order")
async def list_favourites(ctx: Ctx, principal: PrincipalDep) -> dict[str, Any]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(FavouriteVoice).order_by(
                        FavouriteVoice.position, FavouriteVoice.created_at
                    )
                )
            )
            .scalars()
            .all()
        )
        return {"favourites": [_out(r) for r in rows]}


@router.post("/v1/favourites", summary="Star a voice")
async def add_favourite(body: FavouriteIn, ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    async with session_scope() as session:
        existing = await session.get(FavouriteVoice, body.voice_id)
        if existing is not None:
            # Idempotent on purpose: two tabs starring the same voice must not error.
            return {"favourite": _out(existing), "created": False}

        count = len((await session.execute(select(FavouriteVoice.voice_id))).scalars().all())
        if count >= MAX_FAVOURITES:
            raise InvalidRequest(
                f"already at the {MAX_FAVOURITES}-favourite limit; remove one first",
                details={"limit": MAX_FAVOURITES},
            )

        row = FavouriteVoice(
            voice_id=body.voice_id,
            provider=body.provider,
            name=body.name,
            # Newest last: appending keeps a shortlist in the order it was built, which is
            # how people read their own lists.
            position=count,
        )
        session.add(row)
        await session.flush()
        return {"favourite": _out(row), "created": True}


@router.delete("/v1/favourites/{voice_id:path}", summary="Unstar a voice")
async def remove_favourite(voice_id: str, ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    async with session_scope() as session:
        result = await session.execute(
            delete(FavouriteVoice).where(FavouriteVoice.voice_id == voice_id)
        )
        removed = getattr(result, "rowcount", 0) or 0
        if not removed:
            # Not an error: the UI removes optimistically, and a second tab may already
            # have done it.
            return {"removed": False}
        return {"removed": True}


@router.put("/v1/favourites/order", summary="Reorder favourites (list of voice ids)")
async def reorder(body: list[str], ctx: Ctx, principal: AdminDep) -> dict[str, Any]:
    async with session_scope() as session:
        rows = {
            r.voice_id: r for r in (await session.execute(select(FavouriteVoice))).scalars().all()
        }
        unknown = [v for v in body if v not in rows]
        if unknown:
            raise InvalidRequest(
                "ordering references voices that are not favourites",
                details={"unknown": unknown[:10]},
            )
        for position, voice_id in enumerate(body):
            rows[voice_id].position = position
        # Anything the caller omitted keeps a position past the end rather than colliding.
        for offset, voice_id in enumerate(sorted(set(rows) - set(body))):
            rows[voice_id].position = len(body) + offset
        return {"ordered": len(body)}
