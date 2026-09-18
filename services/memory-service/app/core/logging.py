"""结构化 JSON 日志（docs/06 §2.3）。

每条日志自动携带 trace_id（contextvars），与 Langfuse trace、
RFC 7807 错误响应中的 trace_id 保持同一取值，实现全链路关联。
"""

import json
import logging
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

# 全链路追踪 ID：入口中间件写入，日志/trace/错误响应统一读取
trace_id_var: ContextVar[str] = ContextVar("trace_id", default="-")

# 日志中禁止出现的敏感键（纵深防御：调用方也不应传入）
_SENSITIVE_KEYS = {"password", "token", "api_key", "authorization"}


class JsonFormatter(logging.Formatter):
    """将 LogRecord 序列化为单行 JSON，便于采集与检索。"""

    def format(self, record: logging.LogRecord) -> str:
        """输出格式：timestamp level logger trace_id message + extras。"""
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": trace_id_var.get(),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key in payload or key in _SENSITIVE_KEYS:
                continue
            if not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    """初始化 root logger：JSON 流式输出到 stdout。

    Args:
        level: 日志级别名（DEBUG/INFO/...），来自 Settings.log_level。
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # 降低第三方库噪声
    for noisy in ("uvicorn.access", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
