"""MCP server 配置模型（M-09 扩展：热插拔管理的数据底座）。

全局共享（不分 workspace）：登录用户经 /api/mcp/servers 添加后对所有
用户可见；env 种子行（source='env'）每次启动与 MEMORY_SERVICE_MCP_SERVERS_JSON
同步，API 不可删除。
"""

from sqlalchemy import Boolean, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin


class McpServer(UUIDMixin, TimestampMixin, Base):
    """一个已配置的 MCP server（连接参数 + 运行开关）。

    Attributes:
        name: 全局唯一标识（注册名前缀 mcp.<name>.*，限小写字母/数字/中划线）。
        transport: http（streamable HTTP）/ sse（SSE 端点）/ stdio（子进程）。
        url: 远程端点（http/sse；stdio 时为 None）。
        headers: 远程端点自定义请求头（API key 鉴权；读取接口打码不回显原值）。
        command: stdio transport 的启动命令（http/sse 时为 None）。
        args: stdio 命令参数列表。
        env: stdio 子进程额外环境变量（None 时由 SDK 注入最小环境）。
        enabled: 是否启用（False 时启动期跳过连接）。
        source: 'env'（配置种子，随 .env 同步）/ 'user'（API 添加，可删除）。
    """

    __tablename__ = "mcp_servers"

    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    transport: Mapped[str] = mapped_column(String(8), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048))
    headers: Mapped[dict | None] = mapped_column(JSONB)
    command: Mapped[str | None] = mapped_column(String(512))
    args: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    env: Mapped[dict | None] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    source: Mapped[str] = mapped_column(String(8), default="user", nullable=False)
