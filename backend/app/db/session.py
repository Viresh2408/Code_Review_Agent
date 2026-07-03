"""
SQLAlchemy async engine + session factory.

Usage in FastAPI route:
    async with get_session() as session:
        result = await session.execute(select(Repo))

Usage in Celery tasks (sync context):
    Use the sync engine from alembic or a separate sync session.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import get_settings

settings = get_settings()

# ── Async engine ──────────────────────────────────────────────────────────────
engine_kwargs = {
    "echo": settings.app_env == "development",   # SQL logging in dev only
    "pool_pre_ping": True,                       # auto-reconnect on stale connections
}
if "sqlite" not in settings.database_url:
    engine_kwargs["pool_size"] = 10
    engine_kwargs["max_overflow"] = 20

async_engine = create_async_engine(
    settings.database_url,
    **engine_kwargs
)

# ── Session factory ───────────────────────────────────────────────────────────
AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)

# ── Dependency / context manager ─────────────────────────────────────────────
@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Async context manager that yields a session and handles commit/rollback."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
