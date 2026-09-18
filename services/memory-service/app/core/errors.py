"""统一业务异常与 RFC 7807 问题详情响应（docs/06 §2.2）。

约定：
  - 业务代码只抛 AppError 及其子类；未捕获异常由 FastAPI 默认 500 兜底
  - 响应体固定结构：type/title/status/detail/trace_id
"""

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.core.logging import trace_id_var

logger = logging.getLogger(__name__)


class AppError(Exception):
    """业务异常基类。

    Attributes:
        code: 机器可读错误码，同时用作 problem type 后缀（如 permission_denied）。
        http_status: 返回给客户端的 HTTP 状态码。
        detail: 面向用户的中文错误描述。
    """

    def __init__(self, code: str, http_status: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.http_status = http_status
        self.detail = detail


class NotFoundError(AppError):
    """资源不存在或无权查看（统一 404，避免存在性泄露）。"""

    def __init__(self, resource: str = "资源") -> None:
        super().__init__("not_found", 404, f"{resource}不存在")


class PermissionDeniedError(AppError):
    """workspace 归属或角色校验失败（隔离第二道防线抛出点）。"""

    def __init__(self, detail: str = "workspace 不存在或无访问权限") -> None:
        super().__init__("permission_denied", 403, detail)


class ConflictError(AppError):
    """并发冲突（消息版本乐观锁、会话锁等）。"""

    def __init__(self, detail: str = "资源已被并发修改，请重试") -> None:
        super().__init__("conflict", 409, detail)


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """将 AppError 包装为 RFC 7807 JSON 响应并记录日志。

    Args:
        request: 触发异常的请求（用于日志定位）。
        exc: 业务异常实例。

    Returns:
        application/problem+json 风格响应（Content-Type 保持 application/json，
        便于前端统一解析）。
    """
    trace_id = trace_id_var.get()
    logger.warning(
        "app_error",
        extra={"path": request.url.path, "code": exc.code, "status": exc.http_status},
    )
    body: dict[str, Any] = {
        "type": f"https://echodesk.errors/{exc.code}",
        "title": exc.code,
        "status": exc.http_status,
        "detail": exc.detail,
        "trace_id": trace_id,
    }
    return JSONResponse(status_code=exc.http_status, content=body)
