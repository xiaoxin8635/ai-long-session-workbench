"""EchoDesk memory-service 应用入口。

职责（docs/06 §M-01）：
  - 应用工厂 create_app()：lifespan(trace 兜底 flush) → 中间件(trace_id/CORS)
    → 异常处理器 → 路由注册
  - 不在此处放置任何业务逻辑
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.checkpoint import get_agent_runtime
from app.api.routes import (
    auth,
    chat,
    context_debug,
    health,
    knowledge,
    memories,
    sessions,
    tasks,
    tools,
    usage,
    workspaces,
)
from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler
from app.core.logging import setup_logging
from app.core.middleware import TraceIDMiddleware
from app.observability.tracing import shutdown_flush
from app.tools.mcp_client import connect_mcp_tools, disconnect_mcp_tools


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """进程生命周期：启动 checkpoint 池与 MCP 外部工具，退出时逐一释放。

    降级契约：PostgresSaver 不可用降级 InMemorySaver（AgentRuntime.start
    内部处理）；MCP server 连接失败逐条跳过——均不阻塞启动。

    Yields:
        应用存活期；shutdown 段断开 MCP 连接、关闭 checkpoint 池、flush
        Langfuse 批量上报队列（M-12）后返回（未配置项均为 no-op）。
    """
    await get_agent_runtime().start()
    await connect_mcp_tools(get_settings())
    yield
    await disconnect_mcp_tools()
    await get_agent_runtime().close()
    shutdown_flush()


def create_app() -> FastAPI:
    """构建 FastAPI 应用实例（工厂模式，便于测试注入与多环境装配）。

    Returns:
        配置完成的 FastAPI 应用。
    """
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        # 品牌与署名：EchoDesk（基于 Open WebUI 工作台的自研服务端）
        description="AI 长会话知识工作台 · 记忆与上下文核心服务",
        lifespan=_lifespan,
    )

    # ---- 中间件（注册顺序 = 执行顺序：trace_id 最外层）----
    app.add_middleware(TraceIDMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000"],  # Open WebUI 产品壳
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )

    # ---- 统一错误（RFC 7807）----
    app.add_exception_handler(AppError, app_error_handler)

    # ---- 路由 ----
    app.include_router(health.router, tags=["health"])
    app.include_router(auth.router)
    app.include_router(workspaces.router)
    app.include_router(sessions.router)
    app.include_router(chat.router)
    app.include_router(context_debug.router)
    app.include_router(memories.router)
    app.include_router(usage.router)
    app.include_router(knowledge.router)
    app.include_router(tasks.router)
    app.include_router(tools.router)

    return app


app = create_app()
