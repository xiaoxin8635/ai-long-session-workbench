"""MCP server 管理端点（M-09 扩展：热插拔，docs/06 §11）。

  GET    /api/mcp/servers        配置 + 运行态（connected/tools）
  POST   /api/mcp/servers        热添加（试连成功才落库，201；失败 502）
  DELETE /api/mcp/servers/{name} 热移除（断连 + 注销工具 + 删行，204）

安全口径：全局共享模块（所有登录用户可见/可管理，与工具注册表的进程级
语义一致）；新工具默认 risk=external，执行仍走确认流（D1 不放宽）。
env 种子行（source='env'）不可经 API 删除。
"""

from fastapi import APIRouter, Response

from app.core.config import get_settings
from app.core.deps import CurrentUser, DbDep
from app.schemas.mcp import McpServerCreate, McpServerRead
from app.services.mcp_server_service import mcp_server_service

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


@router.get("/servers", response_model=list[McpServerRead], summary="MCP 服务清单")
async def list_servers(user: CurrentUser, db: DbDep) -> list[McpServerRead]:
    """全部 server 配置与运行态（登录即可见）。"""
    return await mcp_server_service.list_servers(db)


@router.post(
    "/servers",
    response_model=McpServerRead,
    status_code=201,
    summary="热添加 MCP 服务",
)
async def add_server(payload: McpServerCreate, user: CurrentUser, db: DbDep) -> McpServerRead:
    """试连并注册工具，成功后落库（工具即刻进入 /api/tools 清单）。

    Raises:
        ConflictError: 409 —— 名称已存在。
        AppError: 422 配置不合法 / 502 连接失败（不落库）。
    """
    row = await mcp_server_service.add(db, get_settings(), payload)
    return mcp_server_service.to_read(row)


@router.delete("/servers/{name}", status_code=204, summary="热移除 MCP 服务")
async def remove_server(name: str, user: CurrentUser, db: DbDep) -> Response:
    """断连 + 注销 mcp.<name>.* 工具 + 删配置行。

    Raises:
        NotFoundError: 404 —— 配置不存在。
        AppError: 400 —— env 种子行不可经 API 删除。
    """
    await mcp_server_service.remove(db, name)
    return Response(status_code=204)
