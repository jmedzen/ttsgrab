"""Async engine/session plumbing."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
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


async def ensure_columns() -> list[str]:
    """Add nullable columns that the models gained since the database was created.

    ``create_all()`` creates missing *tables* and silently ignores a new column on an
    existing table, so an upgraded deployment would run with a schema that no longer
    matches the models and fail at query time with "no such column".

    Alembic stays the deliberate path for operators; this is the safety net that keeps a
    plain upgrade working. It only ever ADDs a nullable column — never drops, renames, or
    rewrites anything, so it cannot lose data.
    """
    engine = get_engine()
    added: list[str] = []
    async with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = await conn.run_sync(_column_names, table.name)
            if not existing:
                continue  # yet to be created; create_all handles it
            for column in table.columns:
                if column.name in existing:
                    continue
                # Nullable-only on purpose: adding NOT NULL without a server default fails
                # on any non-empty table.
                type_sql = _sqlite_type(column.type)
                if type_sql is None:
                    continue
                await conn.execute(
                    sa.text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {type_sql}')
                )
                added.append(f"{table.name}.{column.name}")
    return added


def _column_names(conn, table: str) -> set[str]:
    from sqlalchemy import inspect

    return {c["name"] for c in inspect(conn).get_columns(table)}


def _sqlite_type(col_type) -> str | None:
    """Map a SQLAlchemy type to the SQLite storage class we would have used.

    JSON/ARRAY columns are TEXT in SQLite; rendering the SQLAlchemy type directly produces
    "JSON" which SQLite accepts but which no other tool expects.
    """
    from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Float, Integer, Text, types

    if isinstance(col_type, (Text, types.UnicodeText, types.String)):
        return "TEXT"
    if isinstance(col_type, Boolean):
        return "BOOLEAN"
    if isinstance(col_type, BigInteger):
        return "BIGINT"
    if isinstance(col_type, Integer):
        return "INTEGER"
    if isinstance(col_type, Float):
        return "FLOAT"
    if isinstance(col_type, DateTime):
        return "DATETIME"
    if isinstance(col_type, JSON):
        return "TEXT"
    return None
