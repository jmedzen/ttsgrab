"""Async engine/session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ttsgrab.db.models import Base

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    global _engine, _factory
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_async_engine(
        database_url,
        echo=echo,
        future=True,
        pool_pre_ping=not database_url.startswith("sqlite"),
        connect_args=connect_args,
    )
    _factory = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("database engine not initialised")
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _factory is None:
        raise RuntimeError("database session factory not initialised")
    return _factory


async def create_all() -> None:
    """Fast path for dev/tests. Production uses Alembic."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    factory = session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def dispose() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
