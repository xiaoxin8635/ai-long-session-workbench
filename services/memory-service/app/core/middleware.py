"""TraceID 中间件（docs/06 §2.3）。

每个入站请求：读取或生成 X-Request-ID → 写入 contextvars →
响应头回传同一取值；Langfuse span / 日志 / 错误体全部复用。
"""

from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import trace_id_var

REQUEST_ID_HEADER = "X-Request-ID"


class TraceIDMiddleware(BaseHTTPMiddleware):
    """为每个请求建立全链路 trace_id 上下文。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        """透传上游 Request-ID（如 Open WebUI Pipe 注入），否则生成 UUID4。

        Args:
            request: 入站请求。
            call_next: 下一个处理层。

        Returns:
            带回传 X-Request-ID 头的响应。
        """
        incoming = request.headers.get(REQUEST_ID_HEADER)
        tid = incoming or uuid4().hex
        token = trace_id_var.set(tid)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = tid
            return response
        finally:
            trace_id_var.reset(token)
