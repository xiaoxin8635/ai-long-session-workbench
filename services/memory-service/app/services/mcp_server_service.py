"""MCP server 管理服务（M-09 扩展：热插拔编排）。

职责：DB 配置行与进程级连接池/工具注册表之间的同步编排——
  - bootstrap：启动期把 env 种子同步进 mcp_servers 表，再连接全部启用行
  - add：先试连（失败转 502，不落库）→ 注册工具 → 落库 source='user'
  - remove：断开 + 注销前缀 + 删行（env 种子行拒绝，改 .env 重启才生效）

安全口径：MCP 工具统一按 Settings.mcp_tool_risk（默认 external）分级，
执行前必须用户确认（D1 决策不变，热添加不放宽）。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import AppError, ConflictError, NotFoundError
from app.models.mcp_server import McpServer
from app.repositories import mcp_server_repo
from app.schemas.mcp import McpServerCreate, McpServerRead
from app.tools.mcp_client import (
    McpConnectionError,
    McpServerConfig,
    connect_server,
    connected_servers,
    disconnect_server,
    parse_server_configs,
)

logger = logging.getLogger(__name__)


def _config_from_row(row: McpServer) -> McpServerConfig:
    """把 DB 配置行转回连接配置对象。

    Args:
        row: mcp_servers 表行。

    Returns:
        McpServerConfig 实例。
    """
    return McpServerConfig(
        name=row.name,
        transport=row.transport,
        url=row.url,
        headers=row.headers,
        command=row.command,
        args=list(row.args or []),
        env=row.env,
        enabled=row.enabled,
    )


class McpServerService:
    """MCP server 热插拔编排（无状态，连接池在 mcp_client 模块级）。"""

    async def bootstrap(self, db: AsyncSession, settings: Settings) -> None:
        """启动期同步 env 种子并连接全部启用的 server（lifespan 调用）。

        env 行以 MEMORY_SERVICE_MCP_SERVERS_JSON 为唯一定义源：新增 upsert、
        消失删除；user 行与 env 重名时不劫持（告警跳过种子写入）。
        单个 server 连接失败仅告警跳过（部分降级契约，不阻塞启动）。

        Args:
            db: 数据库会话。
            settings: 全局配置。
        """
        env_configs = parse_server_configs(settings.mcp_servers_json)
        env_names: set[str] = set()
        for config in env_configs:
            env_names.add(config.name)
            existing = await mcp_server_repo.get_by_name(db, config.name)
            if existing is None:
                await mcp_server_repo.create_server(
                    db,
                    name=config.name,
                    transport=config.transport,
                    url=config.url,
                    headers=config.headers,
                    command=config.command,
                    args=list(config.args),
                    env=config.env,
                    source="env",
                )
            elif existing.source == "env":
                existing.transport = config.transport
                existing.url = config.url
                existing.headers = config.headers
                existing.command = config.command
                existing.args = list(config.args)
                existing.env = config.env
                existing.enabled = config.enabled
                await db.flush()
            else:
                logger.warning(
                    "mcp_env_seed_skipped name=%s 与 API 添加的 server 重名，保留 user 行",
                    config.name,
                )
                env_names.discard(config.name)
        await mcp_server_repo.delete_env_absent(db, env_names)

        for row in await mcp_server_repo.list_enabled(db):
            try:
                await connect_server(_config_from_row(row), settings)
            except McpConnectionError as exc:
                logger.warning("mcp_server_connect_skipped server=%s error=%s", row.name, exc)

    async def add(
        self, db: AsyncSession, settings: Settings, payload: McpServerCreate
    ) -> McpServer:
        """热添加 server：试连成功才落库（连接保留，工具即刻可用）。

        Args:
            db: 数据库会话。
            settings: 全局配置（风险分级与超时来源）。
            payload: 添加请求体。

        Returns:
            落库后的 McpServer 行。

        Raises:
            ConflictError: 409 —— 名称已存在。
            AppError: 422 配置与 transport 不匹配 / 502 连接失败。
        """
        if await mcp_server_repo.get_by_name(db, payload.name) is not None:
            raise ConflictError(f"MCP server {payload.name} 已存在")
        try:
            config = McpServerConfig.model_validate(payload.model_dump())
        except Exception as exc:  # pydantic ValidationError → 422
            raise AppError("mcp_config_invalid", 422, f"配置不合法：{exc}") from exc
        try:
            await connect_server(config, settings)
        except McpConnectionError as exc:
            raise AppError("mcp_connect_failed", 502, str(exc)) from exc
        return await mcp_server_repo.create_server(
            db,
            name=payload.name,
            transport=payload.transport,
            url=payload.url,
            headers=payload.headers,
            command=payload.command,
            args=list(payload.args),
            env=payload.env,
            source="user",
        )

    async def remove(self, db: AsyncSession, name: str) -> None:
        """热移除 server：断连 + 注销工具 + 删配置行。

        Args:
            db: 数据库会话。
            name: server 标识。

        Raises:
            NotFoundError: 404 —— 配置不存在。
            AppError: 400 —— env 种子行（改 .env 后重启才移除）。
        """
        row = await mcp_server_repo.get_by_name(db, name)
        if row is None:
            raise NotFoundError("MCP server")
        if row.source == "env":
            raise AppError(
                "mcp_env_managed",
                400,
                "env 来源的 server 请从 MEMORY_SERVICE_MCP_SERVERS_JSON 移除后重启服务",
            )
        await disconnect_server(name)
        await mcp_server_repo.delete_by_name(db, name)

    def to_read(self, row: McpServer) -> McpServerRead:
        """配置行 → 响应体（附运行态 connected/tools；headers 值打码）。

        Args:
            row: mcp_servers 表行。

        Returns:
            McpServerRead 实例。
        """
        snapshot = connected_servers()
        read = McpServerRead.model_validate(row)
        # 密钥不回显：仅保留头名，值统一打码（前端只需展示「带鉴权头」）
        read.headers = {key: "***" for key in row.headers} if row.headers else None
        read.connected = row.name in snapshot
        read.tools = snapshot.get(row.name, [])
        return read

    async def list_servers(self, db: AsyncSession) -> list[McpServerRead]:
        """配置行 + 运行态快照（connected/tools）。

        Args:
            db: 数据库会话。

        Returns:
            McpServerRead 列表（按名称排序）。
        """
        return [self.to_read(row) for row in await mcp_server_repo.list_all(db)]


mcp_server_service = McpServerService()
