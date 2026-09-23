"""MCP server 配置仓储（M-09 扩展：热插拔管理）。

全局共享表（不分 workspace）：只做查询与构造，连接编排与注册表同步
在 mcp_server_service 层。
"""

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mcp_server import McpServer


async def list_all(db: AsyncSession) -> list[McpServer]:
    """全部 server 配置（按名称排序，env 种子在前由前端分组展示）。

    Args:
        db: 数据库会话。

    Returns:
        McpServer 列表。
    """
    result = await db.execute(select(McpServer).order_by(McpServer.name))
    return list(result.scalars().all())


async def list_enabled(db: AsyncSession) -> list[McpServer]:
    """enabled=True 的 server 配置（启动期连接范围）。

    Args:
        db: 数据库会话。

    Returns:
        McpServer 列表。
    """
    result = await db.execute(select(McpServer).where(McpServer.enabled).order_by(McpServer.name))
    return list(result.scalars().all())


async def get_by_name(db: AsyncSession, name: str) -> McpServer | None:
    """按唯一名称取配置。

    Args:
        db: 数据库会话。
        name: server 标识。

    Returns:
        命中的 McpServer；未找到为 None。
    """
    result = await db.execute(select(McpServer).where(McpServer.name == name))
    return result.scalar_one_or_none()


async def create_server(
    db: AsyncSession,
    *,
    name: str,
    transport: str,
    url: str | None,
    command: str | None,
    args: list[str],
    env: dict[str, str] | None,
    source: str,
) -> McpServer:
    """落库一条 server 配置。

    Args:
        db: 数据库会话。
        name: 全局唯一标识。
        transport: http / stdio。
        url: http 端点（可空）。
        command: stdio 命令（可空）。
        args: stdio 参数列表。
        env: stdio 额外环境变量（可空）。
        source: 'env'（配置种子）/ 'user'（API 添加）。

    Returns:
        落库后的 McpServer 对象。
    """
    server = McpServer(
        name=name,
        transport=transport,
        url=url,
        command=command,
        args=list(args),
        env=env,
        enabled=True,
        source=source,
    )
    db.add(server)
    await db.flush()
    return server


async def delete_by_name(db: AsyncSession, name: str) -> None:
    """按名称删除配置（调用方保证已断开连接）。

    Args:
        db: 数据库会话。
        name: server 标识。
    """
    await db.execute(delete(McpServer).where(McpServer.name == name))
    await db.flush()


async def delete_env_absent(db: AsyncSession, keep_names: set[str]) -> None:
    """删除不在 env 配置中的 source='env' 行（启动期同步，env 是唯一定义源）。

    Args:
        db: 数据库会话。
        keep_names: 本次 env 配置里出现的名称集合。
    """
    stmt = delete(McpServer).where(McpServer.source == "env")
    if keep_names:
        stmt = stmt.where(McpServer.name.notin_(keep_names))
    await db.execute(stmt)
    await db.flush()
