"""数据库引擎与会话。"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from .config import settings
from .models import Base


def _engine_kwargs() -> dict:
    kw: dict = {"echo": settings.db_echo, "pool_pre_ping": True, "future": True}
    if settings.database_url.startswith("sqlite"):
        # SQLite 不支持连接池参数
        kw.pop("pool_pre_ping")
        return kw
    kw["pool_size"] = settings.db_pool_size
    kw["max_overflow"] = settings.db_max_overflow
    kw["pool_recycle"] = 1800  # MySQL wait_timeout 常见为 8h，主动回收避免断连
    return kw


engine: AsyncEngine = create_async_engine(settings.database_url, **_engine_kwargs())

if settings.database_url.startswith("sqlite"):
    # SQLite 默认不启用外键，级联删除会静默失效
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_fk_on(dbapi_conn, _record):  # pragma: no cover - 仅本地开发路径
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖。"""
    async with SessionLocal() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """非请求上下文（worker / 启动钩子）使用。"""
    async with SessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """开发/首次启动时建表。生产建议改用 alembic upgrade head。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
