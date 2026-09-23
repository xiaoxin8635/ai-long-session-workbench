"""Agent 图的状态定义与运行时上下文（M-08 M3）。

GraphState 是 LangGraph 图的可序列化状态（checkpoint 持久化的唯一内容），
全部字段为 str/int/list/dict 等基础类型；GraphContext 是请求级运行依赖
（db 会话、用户、回调等），经 config["configurable"] 注入、不进 checkpoint
——恢复（resume）时由恢复端点按新请求重建。
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from app.context.schemas import AssembledContext
from app.llm.client import LLMClient
from app.models.user import User
from app.services.chat_service import ChatService


class GraphState(TypedDict):
    """LangGraph 图状态（docs/06 §10 接口约定）。

    Attributes:
        user_msg_id: 本轮用户消息的数据库 ID（finalize 抽取的 source_message_ids）。
        user_content: 本轮用户消息正文（finalize 抽取管线输入）。
        messages: 图内运行的 LLM 消息（装配产出 + 工具往返追加）。
        answer: 最终回答文本（generate 收敛时写入）。
        tool_calls: 已执行工具的摘要记录（{call_id, tool, status}）。
        pending_calls: generate → tools 的待执行调用（含 external 的审计 call_id）。
        iterations: generate 已执行轮次（工具循环防线，> 8 强制收敛）。
        prompt_tokens: 多轮 LLM 调用的 prompt token 累加。
        completion_tokens: 多轮 LLM 调用的 completion token 累加。
    """

    user_msg_id: str
    user_content: str
    messages: list[dict[str, Any]]
    answer: str
    tool_calls: list[dict[str, Any]]
    pending_calls: list[dict[str, Any]]
    iterations: int
    prompt_tokens: int
    completion_tokens: int


@dataclass
class GraphContext:
    """一次图调用的请求级运行依赖（configurable 注入，不进 checkpoint）。

    Attributes:
        db: 请求级数据库会话。
        ws_id: 所属 workspace。
        user: 当前认证用户。
        session_id: 目标会话。
        llm: 对话 LLM 客户端。
        chat_service: chat 用例编排（load_context 复用 assemble、postprocess
            复用 finalize，保留 M-12 观测 span）。
        user_msg_id: 本轮用户消息 ID（初始 state 同步冗余，防 resume 丢失）。
        tools: OpenAI tools 参数（空列表 = 本轮禁用工具循环）。
        on_delta: 正文增量回调（SSE bridge 推流；非流式传收集器）。
        on_event: 结构化事件回调（如 tool_call 确认请求推送）。
        attachment_ids: 本轮聊天附件的知识文件 ID（load_context 强制注入切片）。
        assembled: load_context 的装配结果（postprocess 用量明细来源）。
    """

    db: AsyncSession
    ws_id: uuid.UUID
    user: User
    session_id: uuid.UUID
    llm: LLMClient
    chat_service: ChatService
    user_msg_id: uuid.UUID
    tools: list[dict[str, Any]]
    on_delta: Callable[[str], Awaitable[None]]
    on_event: Callable[[str, dict[str, Any]], Awaitable[None]]
    attachment_ids: list[uuid.UUID] = field(default_factory=list)
    assembled: AssembledContext | None = field(default=None)


@dataclass(frozen=True)
class AccumulatedUsage:
    """工具循环多轮 LLM 调用的用量累加（鸭子类型适配 usage_repo.record_turn）。

    Attributes:
        prompt_tokens: 各轮 prompt token 之和。
        completion_tokens: 各轮 completion token 之和。
    """

    prompt_tokens: int
    completion_tokens: int
