"""Context Builder 数据结构（M-05，docs/06 §7）。

装配流水线中的三类核心对象：
  - ContextItem：单个候选片段（内容 + 溯源标记 + 淘汰分数）
  - ContextCandidates：一次装配的全部候选（按区块分组）
  - AssembledContext：装配结果（messages + 各区块用量明细）
"""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SectionKey(StrEnum):
    """上下文区块标识（装配顺序即枚举顺序：system → … → tool_results）。"""

    SYSTEM = "system"
    PROCEDURAL = "procedural"
    SEMANTIC = "semantic"
    EPISODIC = "episodic"
    WORKING = "working"
    RAG = "rag"
    TOOL_RESULTS = "tool_results"


@dataclass(frozen=True)
class ContextItem:
    """单个候选片段（记忆条目/RAG chunk/工具结果统一抽象）。

    Attributes:
        content: 片段正文。
        source: 溯源标记（记忆 key / chunk 定位 / 工具调用 ID），注入结构化标签。
        score: 淘汰分数（区块内超预算时低分先丢；working 区块不使用）。
    """

    content: str
    source: str
    score: float = 0.0


@dataclass(frozen=True)
class ContextCandidates:
    """一次装配的全部候选输入（各区块独立，裁剪互不影响）。

    Attributes:
        system_prompt: 系统提示（角色 + 记忆使用说明），固定注入不裁剪。
        procedural: 偏好/工作流记忆（procedural memory）。
        semantic: 事实记忆（semantic memory）。
        episodic: 会话摘要（本会话滚动摘要 + 跨会话摘要检索）。
        working: 近期消息窗口（role/content 字典，时间正序，末条即当前问题）。
        rag: 知识库片段（M-07 接入）。
        tool_results: 当轮工具结果（M-09 接入）。
    """

    system_prompt: str
    procedural: tuple[ContextItem, ...] = ()
    semantic: tuple[ContextItem, ...] = ()
    episodic: tuple[ContextItem, ...] = ()
    working: tuple[dict[str, Any], ...] = ()
    rag: tuple[ContextItem, ...] = ()
    tool_results: tuple[ContextItem, ...] = ()


@dataclass(frozen=True)
class SectionUsage:
    """单区块装配用量（preview 端点与统计口径）。

    Attributes:
        key: 区块标识。
        budget_tokens: 区块预算（system 为 -1 表示不裁剪）。
        included: 实际纳入条数。
        dropped: 裁剪丢弃条数。
        tokens: 实际 token 占用。
    """

    key: SectionKey
    budget_tokens: int
    included: int
    dropped: int
    tokens: int


@dataclass(frozen=True)
class AssembledContext:
    """装配结果：可直接送 LLM 的 messages + 用量明细。

    Attributes:
        messages: OpenAI 格式消息列表（一条合成 system + working 的 role 消息）。
        sections: 各区块用量（装配顺序）。
        total_tokens: 全部区块 token 总和。
    """

    messages: list[dict[str, str]] = field(default_factory=list)
    sections: tuple[SectionUsage, ...] = ()
    total_tokens: int = 0
