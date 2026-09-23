"""MCP server 管理端点测试（M-09 扩展：热插拔，docs/06 §11）。

覆盖：
  - 鉴权（401）与清单空态
  - 热添加成功流（201 → 清单可见 → 重名 409 → 删除 204 → 404）
  - 配置不合法 422（http 缺 url）
  - 试连失败 502 且不落库（必拒绝端口，真实 connect_server）
  - env 种子行不可经 API 删除（400）
  - bootstrap：env 配置种子进表 / 消失后清理

连接层用 monkeypatch 替身（不出网、不起子进程）；502 用例复用
test_mcp_client 的必拒绝端口口径。
"""

import pytest
from httpx import AsyncClient

import app.services.mcp_server_service as svc_mod
from app.core.config import Settings, get_settings
from app.db.session import get_session_factory
from app.repositories import mcp_server_repo
from app.services.mcp_server_service import mcp_server_service
from app.tools.mcp_client import McpServerConfig

_PASSWORD = "passw0rd123"
_REFUSED_URL = "http://127.0.0.1:1/mcp"


async def _auth(client: AsyncClient, username: str = "mcpadmin") -> dict:
    """注册登录，返回 Bearer headers。"""
    await client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _fake_connect(config: McpServerConfig, settings: Settings) -> None:
    """connect_server 替身：直接成功（不建真实连接）。"""
    return None


@pytest.fixture
def fake_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    """把服务层的 connect_server 换成替身。"""
    monkeypatch.setattr(svc_mod, "connect_server", _fake_connect)


async def test_list_requires_auth(client: AsyncClient) -> None:
    """未带令牌 401。"""
    resp = await client.get("/api/mcp/servers")
    assert resp.status_code == 401


async def test_add_list_remove_flow(client: AsyncClient, fake_connect: None) -> None:
    """热添加 → 清单 → 重名 409 → 删除 204 → 再删 404。"""
    headers = await _auth(client)
    assert (await client.get("/api/mcp/servers", headers=headers)).json() == []

    payload = {"name": "demo", "transport": "stdio", "command": "echo", "args": ["hi"]}
    created = await client.post("/api/mcp/servers", json=payload, headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "demo"
    assert body["source"] == "user"
    assert body["connected"] is False  # 替身不入连接池，运行态如实反映
    assert body["tools"] == []

    listing = await client.get("/api/mcp/servers", headers=headers)
    assert [item["name"] for item in listing.json()] == ["demo"]

    duplicate = await client.post("/api/mcp/servers", json=payload, headers=headers)
    assert duplicate.status_code == 409

    removed = await client.delete("/api/mcp/servers/demo", headers=headers)
    assert removed.status_code == 204
    assert (await client.get("/api/mcp/servers", headers=headers)).json() == []
    assert (await client.delete("/api/mcp/servers/demo", headers=headers)).status_code == 404


async def test_add_invalid_config_422(client: AsyncClient, fake_connect: None) -> None:
    """http 缺 url：服务层 McpServerConfig 校验转 422。"""
    headers = await _auth(client)
    resp = await client.post(
        "/api/mcp/servers", json={"name": "nourl", "transport": "http"}, headers=headers
    )
    assert resp.status_code == 422
    assert resp.json()["title"] == "mcp_config_invalid"


async def test_add_connect_failure_502_not_persisted(client: AsyncClient) -> None:
    """试连失败（必拒绝端口）：502 且不落库（真实 connect_server）。"""
    headers = await _auth(client)
    resp = await client.post(
        "/api/mcp/servers",
        json={"name": "refused", "transport": "http", "url": _REFUSED_URL},
        headers=headers,
    )
    assert resp.status_code == 502
    assert resp.json()["title"] == "mcp_connect_failed"
    assert (await client.get("/api/mcp/servers", headers=headers)).json() == []


async def test_env_row_delete_rejected(client: AsyncClient) -> None:
    """env 种子行删除返回 400（引导改 .env）。"""
    factory = get_session_factory()
    async with factory() as db:
        await mcp_server_repo.create_server(
            db,
            name="seeded",
            transport="stdio",
            url=None,
            command="echo",
            args=[],
            env=None,
            source="env",
        )
        await db.commit()
    headers = await _auth(client)
    resp = await client.delete("/api/mcp/servers/seeded", headers=headers)
    assert resp.status_code == 400
    assert resp.json()["title"] == "mcp_env_managed"


async def test_bootstrap_seeds_and_cleans_env_rows(client: AsyncClient, fake_connect: None) -> None:
    """bootstrap：env 配置 upsert 进表（source=env）；配置消失后行被清理。"""
    seeded = get_settings().model_copy(
        update={"mcp_servers_json": '[{"name":"seed","transport":"stdio","command":"echo"}]'}
    )
    factory = get_session_factory()
    async with factory() as db:
        await mcp_server_service.bootstrap(db, seeded)
        await db.commit()
        rows = await mcp_server_repo.list_all(db)
        assert [(row.name, row.source) for row in rows] == [("seed", "env")]

        emptied = seeded.model_copy(update={"mcp_servers_json": ""})
        await mcp_server_service.bootstrap(db, emptied)
        await db.commit()
        assert await mcp_server_repo.list_all(db) == []
