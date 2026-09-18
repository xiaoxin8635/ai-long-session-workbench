"""EchoDesk memory-service 应用入口。

职责（docs/06 §M-01）：
  - 应用工厂 create_app()：中间件(trace_id/CORS) → 异常处理器 → 路由注册
  - 不在此处放置任何业务逻辑
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import health
from app.core.config import get_settings
from app.core.errors import AppError, app_error_handler
from app.core.logging import setup_logging
from app.core.middleware import TraceIDMiddleware


def create_app() -> FastAPI:
    """构建 FastAPI 应用实例（工厂模式，便于测试注入与多环境装配）。

    Returns:
        配置完成的 FastAPI 应用。

    Notes:
        启动/关闭时的资源管理（DB 引擎、Redis 连接池）在 M-02
        引入 lifespan 后挂载，当前阶段健康检查按需建连。
    """
    settings = get_settings()
    setup_logging(settings.log_level)
    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        # 品牌与署名：EchoDesk（基于 Open WebUI 工作台的自研服务端）
        description="AI 长会话知识工作台 · 记忆与上下文核心服务",
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

    return app


app = create_app()
