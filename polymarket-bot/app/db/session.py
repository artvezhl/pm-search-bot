"""Database engines and session factories (async + sync)."""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.config import get_settings

_settings = get_settings()

async_engine = create_async_engine(
    _settings.postgres_dsn,
    # Celery tasks call asyncio.run(...) and therefore create fresh event loops.
    # asyncpg connections cannot be safely reused across different loops.
    # NullPool avoids cross-loop reuse and fixes "Future attached to a different loop".
    poolclass=NullPool,
    future=True,
)
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
)

sync_engine = create_engine(
    _settings.postgres_dsn_sync,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)
SyncSessionLocal = sessionmaker(bind=sync_engine, expire_on_commit=False, autoflush=False)


async def get_async_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with AsyncSessionLocal() as session:
        yield session
