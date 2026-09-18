"""数据库引擎与会话管理。

运行时使用 asyncpg 异步驱动；引擎与会话工厂在首次调用时懒创建，
由 FastAPI 依赖（app/core/deps.py）统一发放 AsyncSession。
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

# 模块级引擎缓存（进程内单例；测试中通过依赖注入替换）
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """返回进程级异步引擎（不存在则创建）。

    连接池参数来自 Settings（db_pool_size / db_max_overflow）。
    """
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_pre_ping=True,  # 取连接前探活，避免拿到已被 PG 重置的连接
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回进程级会话工厂。"""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(),
            expire_on_commit=False,  # 提交后仍可读对象属性（响应组装需要）
            autoflush=False,
        )
    return _session_factory


async def session_scope() -> AsyncIterator[AsyncSession]:
    """提供一次请求级会话：正常提交、异常回滚、最终关闭。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def dispose_engine() -> None:
    """释放引擎（应用关闭/测试重置时调用；幂等）。"""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
