"""pytest 全局夹具。

测试策略（docs/02 §6）：
  - 单元/API 冒烟不依赖外部服务（覆盖 get_db/get_redis 等依赖）
  - 集成测试（M-02 起）使用 docker compose 提供的真实 PG（workbench_test 库）

集成测试引导顺序（关键）：
  1. 在导入任何 app.* 模块【之前】设置 MEMORY_SERVICE_DATABASE_URL，
     指向 compose 暴露在 localhost:5432 的 workbench_test 库；
     密码从 deploy/.env 读取（POSTGRES_PASSWORD）。
  2. 每个测试用例前 create_all（幂等）+ 清空全部表，保证用例间隔离。
"""

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

# ---- 测试数据库环境引导（必须先于 app 导入执行）----
_DEPLOY_ENV = Path(__file__).resolve().parents[3] / "deploy" / ".env"


def _load_deploy_env() -> dict[str, str]:
    """解析 deploy/.env 为字典（不存在时返回空映射，走 Settings 默认值）。"""
    if not _DEPLOY_ENV.exists():
        return {}
    values: dict[str, str] = {}
    for line in _DEPLOY_ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


_DEPLOY = _load_deploy_env()
_PG_USER = _DEPLOY.get("POSTGRES_USER", "workbench")
_PG_PASSWORD = _DEPLOY.get("POSTGRES_PASSWORD", "workbench")

os.environ.setdefault(
    "MEMORY_SERVICE_DATABASE_URL",
    f"postgresql+asyncpg://{_PG_USER}:{_PG_PASSWORD}@localhost:5432/workbench_test",
)
# 测试用 JWT 密钥（≥32 字节，避免 pyjwt 弱密钥告警；生产密钥由部署环境注入）
_TEST_JWT_SECRET = "test-only-jwt-secret-0123456789abcdef012345678"
os.environ.setdefault("MEMORY_SERVICE_JWT_SECRET", _TEST_JWT_SECRET)

from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.core.deps import close_redis  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.db.session import dispose_engine, get_engine  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def app() -> FastAPI:
    """构造测试应用实例（使用 workbench_test 库）。"""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """不经过网络的 ASGI 测试客户端。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
async def _database() -> AsyncIterator[None]:
    """集成测试数据隔离：建表（幂等）+ 清空全部表 → 测试 → 清理资源。

    Windows + asyncpg 关键点：pytest-asyncio 每个用例使用新事件循环，
    而引擎连接池 / Redis 客户端是进程级单例、绑定创建时的循环；
    因此每个用例结束后必须 dispose 并清空缓存，下个用例在新循环上重建。

    Uses:
        autouse —— 所有测试默认获得干净数据库。
    """
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())
    yield
    await close_redis()
    await engine.dispose()
    dispose_engine()
