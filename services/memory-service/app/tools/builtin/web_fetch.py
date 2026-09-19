"""网页抓取工具（M-09，external 风险）：web.fetch。

真实出网动作（读取任意外部 URL），必须经用户确认后执行——承载 M-09
确认流 DoD（未确认 60s 超时拒绝）。抓取限额：单次超时与响应体大小上限
均由 Settings 控制，HTML 抽取为纯文本（跳过 script/style）。
"""

import uuid
from html.parser import HTMLParser

import httpx
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.enums import ToolRiskLevel
from app.models.user import User
from app.tools.registry import ToolDefinition, ToolRegistry, default_registry

# 不参与正文抽取的标签（脚本/样式/模板类）
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head"})


class _TextExtractor(HTMLParser):
    """从 HTML 抽取可见正文文本（跳过 script/style 等，压缩空白）。"""

    def __init__(self) -> None:
        """初始化收集器。"""
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """进入跳过标签时压栈计数。

        Args:
            tag: 标签名。
            attrs: 属性列表（本工具不使用）。
        """
        if tag in _SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        """离开跳过标签时弹栈。

        Args:
            tag: 标签名。
        """
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        """收集可见文本（跳过区间内的数据丢弃）。

        Args:
            data: 文本片段。
        """
        if self._skip_depth == 0 and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        """返回拼接后的正文（换行连接）。"""
        return "\n".join(self._parts)


class WebFetchArgs(BaseModel):
    """web.fetch 参数。"""

    url: HttpUrl = Field(description="目标网址（http/https）")
    max_chars: int = Field(default=4000, ge=200, le=20000, description="返回正文上限")


def _extract_text(body: str) -> str:
    """HTML → 纯文本；非 HTML 原样返回（交由上层截断）。

    Args:
        body: 响应正文。

    Returns:
        抽取后的文本。
    """
    extractor = _TextExtractor()
    try:
        extractor.feed(body)
        extractor.close()
        text = extractor.text()
        return text if text else body
    except Exception:  # HTMLParser 对畸形页面的容错：解析失败退回原文
        return body


async def _fetch(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    session_id: uuid.UUID | None,
    args: WebFetchArgs,
) -> dict:
    """抓取 URL 正文（流式读取，响应体超限即断开）。"""
    settings = get_settings()
    # HttpUrl 已约束 scheme 为 http/https；follow_redirects 保持目标可达性
    async with httpx.AsyncClient(
        timeout=settings.web_fetch_timeout_seconds, follow_redirects=True
    ) as client:
        async with client.stream("GET", str(args.url)) as resp:
            status_code = resp.status_code
            content_type = resp.headers.get("content-type", "")
            collected: list[bytes] = []
            received = 0
            async for chunk in resp.aiter_bytes():
                received += len(chunk)
                collected.append(chunk)
                if received >= settings.web_fetch_max_bytes:
                    break
            body = b"".join(collected).decode(resp.charset_encoding or "utf-8", errors="replace")
    if "html" in content_type.lower():
        text = _extract_text(body)
    else:
        text = body
    truncated = len(text) > args.max_chars
    return {
        "url": str(args.url),
        "status_code": status_code,
        "content_type": content_type,
        "truncated": truncated,
        "text": text[: args.max_chars],
    }


def register(registry: ToolRegistry) -> None:
    """向注册表登记网页抓取工具（builtin 包导入时调用）。

    Args:
        registry: 目标注册表。
    """
    registry.register(
        ToolDefinition(
            name="web.fetch",
            description="抓取指定 URL 的网页正文（真实出网动作，需用户确认后执行）",
            risk=ToolRiskLevel.EXTERNAL,
            args_model=WebFetchArgs,
            handler=_fetch,
        )
    )


register(default_registry)
