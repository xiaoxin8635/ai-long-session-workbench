"""pytest 全局夹具。

测试策略（docs/02 §6）：
  - 单元/API 冒烟不依赖外部服务（覆盖 get_db/get_redis 等依赖）
  - 集成测试（M-02 起）使用 docker compose 起真实依赖
"""

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.main import create_app


@pytest.fixture
def app() -> FastAPI:
    """构造测试应用实例。"""
    return create_app()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """不经过网络的 ASGI 测试客户端。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
