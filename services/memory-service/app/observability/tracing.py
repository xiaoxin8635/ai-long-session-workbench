"""Langfuse 追踪薄封装（M-12，docs/06 §14 埋点矩阵的统一入口）。

设计要点：
  - 懒初始化 + no-op 降级：Settings 的 langfuse_public_host / public_key /
    secret_key 任一为空 → 客户端为 None，span()/turn()/generation() 退化为
    纯 no-op（对齐 embedding/rerank 的降级模式；测试环境默认关闭，业务零感知）
  - 父子传播：SDK 底座为 OpenTelemetry（contextvars），嵌套 with 天然挂父子，
    asyncio.create_task 继承调用点的上下文快照 → 后台抽取/确认任务的 span
    自动挂在触发的 chat.turn 之下
  - turn()：chat 轮次根 span；trace_id 关联请求中间件的 X-Request-ID
    （uuid4().hex → 标准 UUID 形态，Langfuse 要求 UUID 字符串），session/user
    经 propagate_attributes 提升为 trace 属性（多轮聚合同一会话）
  - 上报安全：SDK 后台线程批量上报且内部吞异常；业务侧唯一约定是进程退出前
    调用 shutdown_flush() 兜底（main.py lifespan）。对话正文只上报截断片段，
    其余以结构化指标（tokens/ttfb/区块明细）为主

埋点矩阵（docs/06 §14）：
  chat.turn → context.assemble / memory.retrieve / rag.search / llm.call /
  memory.extract（后台）；tool.invoke 为独立根（工具端点/确认流后台）
"""

import logging
import uuid as uuid_mod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.core.config import get_settings
from app.core.logging import trace_id_var

logger = logging.getLogger(__name__)

# 对话正文上报截断上限（观测排障够用即可，不全量复制业务数据）
_SNIPPET_MAX_CHARS = 500

# 进程级客户端与初始化标记（失败不重试：配置不变结果不变）
_client: Any | None = None
_client_initialized = False


class _NullObservation:
    """未配置 Langfuse 时的空观察对象（update 全部 no-op，调用方零分支）。"""

    def update(self, **_: Any) -> "_NullObservation":
        """忽略任意更新（no-op，签名与 SDK 观察对象对齐）。

        Returns:
            自身（支持链式调用）。
        """
        return self


def snippet(text: str | None, limit: int = _SNIPPET_MAX_CHARS) -> str:
    """截断对话正文片段用于上报（防全量复制，超限标注原始长度）。

    Args:
        text: 原文。
        limit: 截断上限（字符）。

    Returns:
        截断后的片段；None 入参返回空串。
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"…[已截断，原始 {len(text)} 字符]"


def get_client() -> Any | None:
    """返回进程级 Langfuse 客户端（按 Settings 懒创建）。

    Returns:
        已初始化的 Langfuse 客户端；未配置或初始化失败返回 None（no-op 模式）。
    """
    global _client, _client_initialized
    if not _client_initialized:
        _client_initialized = True
        settings = get_settings()
        if (
            settings.langfuse_public_host
            and settings.langfuse_public_key
            and settings.langfuse_secret_key
        ):
            try:
                from langfuse import Langfuse

                _client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_public_host,
                )
                logger.info("langfuse_tracing_enabled host=%s", settings.langfuse_public_host)
            except Exception as exc:  # 初始化失败降级为 no-op，绝不阻断业务
                logger.warning("langfuse_init_failed tracing_disabled error=%s", exc)
                _client = None
    return _client


def reset_client() -> None:
    """清空客户端缓存（配置变更/测试隔离时调用）。"""
    global _client, _client_initialized
    _client = None
    _client_initialized = False


def _resolve_trace_id(trace_id: str | None) -> str:
    """归一 trace id 为 32 位无连字符 hex（SDK 以 int(x, 16) 解析）。

    Args:
        trace_id: 显式传入值；None 时取请求中间件的 trace_id_var。

    Returns:
        32 位 hex 字符串（uuid4().hex 形态）；来源缺失/非法时新生成，
        保证 span 树总有所属 trace。注意：Langfuse SDK 内部用
        int(trace_id, 16) 解析，带连字符的标准 UUID 串会抛 ValueError，
        因此必须用 .hex 输出而非 str(UUID(...))。
    """
    tid = trace_id or trace_id_var.get()
    try:
        return uuid_mod.UUID(str(tid)).hex
    except (ValueError, TypeError):
        return uuid_mod.uuid4().hex


@contextmanager
def turn(
    *,
    session_id: str | None = None,
    user_id: str | None = None,
    tags: list[str] | None = None,
    **meta: Any,
) -> Iterator[Any]:
    """chat 轮次根 span（trace_id 自动取请求中间件的 trace_id_var）。

    Args:
        session_id: EchoDesk 会话 ID（trace 属性：多轮对话聚合同一会话）。
        user_id: 用户 ID（trace 属性）。
        tags: trace 标签（如 ["chat", "m1-direct"]）。
        **meta: 根 span 的 metadata 键值对（workspace_id / stream 等）。

    Yields:
        观察对象（未配置时为 _NullObservation；体内可 update() 补充）。
    """
    client = get_client()
    if client is None:
        yield _NullObservation()
        return
    from langfuse import propagate_attributes
    from langfuse.types import TraceContext

    with propagate_attributes(
        session_id=session_id,
        user_id=user_id,
        tags=tags,
        trace_name="chat.turn",
    ):
        with client.start_as_current_observation(
            name="chat.turn",
            as_type="span",
            metadata=meta or None,
            trace_context=TraceContext(trace_id=_resolve_trace_id(None)),
        ) as obs:
            yield obs


@contextmanager
def span(name: str, **meta: Any) -> Iterator[Any]:
    """通用子 span（docs/06 §14 埋点矩阵的默认形态）。

    Args:
        name: span 名（context.assemble / memory.retrieve / tool.invoke 等）。
        **meta: metadata 键值对（Langfuse 面板展示；动态指标在体内 update）。

    Yields:
        观察对象（未配置时为 _NullObservation；体内可 update() 补充）。
    """
    client = get_client()
    if client is None:
        yield _NullObservation()
        return
    with client.start_as_current_observation(
        name=name, as_type="span", metadata=meta or None
    ) as obs:
        yield obs


@contextmanager
def generation(model: str | None, **meta: Any) -> Iterator[Any]:
    """LLM 调用 span（as_type=generation：UI 提供 model/usage 专属面板）。

    Args:
        model: 模型名（qwen-plus 等；未知时传 None 由体内 update 补）。
        **meta: metadata 键值对（stream / ttfb_ms / usage 在体内 update）。

    Yields:
        观察对象（未配置时为 _NullObservation；体内 update usage_details）。
    """
    client = get_client()
    if client is None:
        yield _NullObservation()
        return
    with client.start_as_current_observation(
        name="llm.call",
        as_type="generation",
        model=model,
        metadata=meta or None,
    ) as obs:
        yield obs


def shutdown_flush() -> None:
    """进程退出前兜底 flush（main.py lifespan shutdown 调用；幂等安全）。"""
    client = get_client()
    if client is None:
        return
    try:
        client.shutdown()
    except Exception as exc:  # 退出路径只记录，不外抛
        logger.warning("langfuse_shutdown_failed error=%s", exc)
