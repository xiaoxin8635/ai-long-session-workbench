"""MCP server 管理 API 模型（M-09 扩展：热插拔）。

请求体直接复用 mcp_client.McpServerConfig 的字段约束（name 模式/transport
枚举/参数匹配校验），响应体附带运行态（connected/tools）。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class McpServerCreate(BaseModel):
    """添加 MCP server 请求体（POST /api/mcp/servers）。

    字段约束与 McpServerConfig 对齐：name 限小写字母/数字/中划线，
    http 必填 url、stdio 必填 command（服务层校验）。
    """

    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
        description="全局唯一标识（工具注册名前缀 mcp.<name>.*）",
    )
    transport: str = Field(pattern="^(http|stdio)$", description="http / stdio")
    url: str | None = Field(default=None, max_length=2048, description="http 端点")
    command: str | None = Field(default=None, max_length=512, description="stdio 启动命令")
    args: list[str] = Field(default_factory=list, description="stdio 命令参数")
    env: dict[str, str] | None = Field(default=None, description="stdio 额外环境变量")


class McpServerRead(BaseModel):
    """server 配置 + 运行态（GET /api/mcp/servers 条目）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    transport: str
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    enabled: bool
    source: str = Field(description="env=配置种子（不可删）/ user=API 添加")
    created_at: datetime
    connected: bool = Field(default=False, description="当前长连接是否存活")
    tools: list[str] = Field(default_factory=list, description="已注册工具名清单")
