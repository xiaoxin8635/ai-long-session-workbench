"""EchoDesk memory-service 应用入口。

职责（docs/06 §M-01）：
  - 应用工厂 create_app()：lifespan(trace 兜底 flush) → 中间件(trace_id/CORS)
    → 异常处理器 → 路由注册
  - 不在此处放置任何业务逻辑
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.agent.checkpoint import get_agent_runtime
from app.api.routes import (
    auth,
    chat,
    context_debug,
    health,
    knowledge,
    mcp,
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
from app.db.session import session_scope
from app.llm.embeddings import EmbeddingError, get_embedding_client
from app.llm.rerank import RerankError, get_rerank_client
from app.observability.tracing import shutdown_flush
from app.services.mcp_server_service import mcp_server_service
from app.tools.mcp_client import disconnect_mcp_tools

logger = logging.getLogger(__name__)

# 预热重试次数与间隔：embedding 容器可能晚于本服务就绪，退避重试等待其可用
_WARMUP_MAX_ATTEMPTS = 6
_WARMUP_RETRY_SECONDS = 3.0


async def _warmup_models() -> None:
    """后台预热 embedding/rerank 模型，消除冷启动首次推理尖峰。

    背景：infinity（CPU）冷启动首次推理可达 ~10s，会顶穿前端超时导致
    “请求超时”。启动后台预热打一次 dummy 请求让模型常驻内存，首个真实
    请求即走热态（实测预热后装配预览 ~0.6s）。降级契约与 MCP/checkpoint
    一致：embedding 未配置则整体跳过，任何失败仅记日志、绝不阻塞启动。
    """
    try:
        embedding = get_embedding_client()
    except EmbeddingError:
        logger.info("model_warmup_skipped embedding 未配置")
        return

    for attempt in range(_WARMUP_MAX_ATTEMPTS):
        try:
            await embedding.embed(["预热"])
        except EmbeddingError:
            await asyncio.sleep(_WARMUP_RETRY_SECONDS)  # embedding 服务可能晚就绪
        else:
            logger.info("model_warmup_embedding_done attempt=%d", attempt)
            break
    else:
        logger.warning("model_warmup_embedding_skipped 重试耗尽，首次请求可能较慢")
        return

    # rerank 预热（可选增强项，未配置/失败均忽略，不影响 embedding 预热成果）
    try:
        await get_rerank_client().rerank("预热", ["甲", "乙"])
        logger.info("model_warmup_rerank_done")
    except RerankError:
        logger.info("model_warmup_rerank_skipped rerank 未配置或不可用")


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
    # MCP：env 种子同步进 mcp_servers 表后按 DB 行连接（支持运行期热插拔）
    try:
        async for db in session_scope():
            await mcp_server_service.bootstrap(db, get_settings())
    except Exception as exc:
        logger.warning("mcp_bootstrap_skipped error=%s（不阻塞启动）", exc)
    # 后台预热推理模型（不阻塞启动/健康检查）；shutdown 时取消未完成任务
    warmup_task = asyncio.create_task(_warmup_models())
    yield
    warmup_task.cancel()
    with suppress(asyncio.CancelledError):
        await warmup_task
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
    app.include_router(mcp.router)

    return app


app = create_app()
