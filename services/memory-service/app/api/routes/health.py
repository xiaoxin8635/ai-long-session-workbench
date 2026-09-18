"""健康检查端点（docs/06 §M-01）。

/healthz：进程存活（不查依赖，供编排层存活探针）；
/readyz ：就绪检查（PG SELECT 1 + Redis PING 全通过才 200）。
"""

import logging

from fastapi import APIRouter, HTTPException
from redis.exceptions import RedisError
from sqlalchemy import text

from app.core.deps import get_redis
from app.db.session import get_engine

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """存活探针：仅确认进程可响应。"""
    return {"status": "ok", "service": "memory-service"}


@router.get("/readyz")
async def readyz() -> dict[str, str]:
    """就绪探针：验证 PostgreSQL 与 Redis 可用。

    Raises:
        HTTPException(503): 任一依赖不可用时返回 503（由探测逻辑直接构造）。
    """
    checks: dict[str, str] = {}

    # ---- PostgreSQL ----
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001 —— 探针需捕获一切依赖故障
        logger.error("readyz postgres failed", exc_info=exc)
        checks["postgres"] = f"error: {type(exc).__name__}"

    # ---- Redis ----
    try:
        if not await get_redis().ping():
            raise RedisError("ping 返回 False")
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        logger.error("readyz redis failed", exc_info=exc)
        checks["redis"] = f"error: {type(exc).__name__}"

    if any(v != "ok" for v in checks.values()):
        raise HTTPException(status_code=503, detail=checks)
    return {"status": "ready", **checks}
