"""Async database engine, session factory, and schema bootstrap.

The engine is created lazily from ``DATABASE_URL`` and can be replaced wholesale
(``set_engine``) so tests run against SQLite in memory without a server.

Schema portability note: the tables deliberately use ``JSON`` rather than
PostgreSQL's ``JSONB``/``ARRAY`` so the very same models run on SQLite in tests
and on PostgreSQL in production. That keeps the Phase 2 test suite server-free
while production stays on PostgreSQL, as the spec requires.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base for every ORM table."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def create_engine(settings: Settings | None = None) -> AsyncEngine:
    """Build an engine for ``settings.database_url``."""
    resolved = settings or get_settings()
    url = resolved.database_url
    kwargs: dict[str, Any] = {"echo": False, "future": True}
    if url.startswith("sqlite"):
        # An in-memory SQLite database must be shared across connections.
        from sqlalchemy.pool import StaticPool

        kwargs["poolclass"] = StaticPool
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        kwargs["pool_pre_ping"] = True
    return create_async_engine(url, **kwargs)


def set_engine(engine: AsyncEngine) -> None:
    """Install ``engine`` as the process-wide engine (used by tests)."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False)


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    """The process-wide engine, created on first use."""
    global _engine
    if _engine is None:
        set_engine(create_engine(settings))
    return _engine


def get_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """The process-wide session factory."""
    if _session_factory is None:
        get_engine(settings)
    assert _session_factory is not None  # set by get_engine
    return _session_factory


@asynccontextmanager
async def session_scope(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """A transactional session: commits on success, rolls back on error."""
    factory = get_session_factory(settings)
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all(settings: Settings | None = None) -> None:
    """Create every table (used by tests and first-run convenience).

    Alembic owns schema changes in real deployments; this exists so a fresh
    checkout can run without a migration step.
    """
    from app.models import hot_content as _hot  # noqa: F401  (register tables)
    from app.models import topic_group as _topic  # noqa: F401

    engine = get_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("database schema ensured (%d tables)", len(Base.metadata.tables))


async def drop_all(settings: Settings | None = None) -> None:
    """Drop every table (tests only)."""
    engine = get_engine(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def dispose_engine() -> None:
    """Close the pool and forget the singletons."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
