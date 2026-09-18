"""FastAPI 依赖注入：数据库会话、Redis 客户端（docs/06 §M-01）。"""

from collections.abc import AsyncIterator

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import session_scope


async def get_db() -> AsyncIterator[AsyncSession]:
    """请求级数据库会话依赖：正常提交、异常回滚。"""
    async for session in session_scope():
        yield session


_redis_client: Redis | None = None


def get_redis() -> Redis:
    """返回进程级 Redis 客户端（异步，decode_responses=True）。

    Working Memory（最近 N 轮窗口）与会话锁的存储入口。
    """
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
    return _redis_client


async def close_redis() -> None:
    """关闭 Redis 连接（应用关闭/测试重置时调用；幂等）。"""
    global _redis_client
    if _redis_client is not None:
        await _redis_client.aclose()
        _redis_client = None
