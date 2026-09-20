"""记忆抽取/检索管线数据结构（M-04，docs/06 §6）。

写路径：ExtractedFact（LLM 抽取原始输出）→ MemoryCandidate（打分与向量
就绪后）→ 入库；冲突仲裁用 ConflictAction / ConflictOutcome。
读路径：ScoredMemory（带相似度与综合分的检索结果）。
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from app.models.enums import MemoryType
from app.models.memory import Memory


@dataclass(frozen=True)
class ExtractedFact:
    """LLM 抽取的单条候选事实（结构化输出元素）。

    Attributes:
        key: 结构化主题键（如 skill.langgraph），去重与冲突定位依据。
        content: 事实正文（完整、自包含的陈述句）。
        memory_type: 记忆类型（semantic 事实 / procedural 偏好）。
        confidence: LLM 自评置信度 0~1。
        importance: 重要性评分 0~1（检索重排权重之一）。
        ttl_days: 建议有效期天数；None 表示长期有效。
        source_message_ids: 支撑该事实的消息 ID 列表（可追溯）。
    """

    key: str
    content: str
    memory_type: MemoryType
    confidence: float
    importance: float
    ttl_days: int | None = None
    source_message_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryCandidate:
    """打分与向量化就绪后的入库候选。

    Attributes:
        fact: 原始抽取事实。
        embedding: content 的稠密向量；embedding 服务不可用时为 None（降级入库）。
        expires_at: TTL 到期时间；长期记忆为 None。
    """

    fact: ExtractedFact
    embedding: list[float] | None
    expires_at: datetime | None


@dataclass(frozen=True)
class Duplicate:
    """去重命中的既有记忆。

    Attributes:
        memory: 库中的旧记忆条目。
        similarity: 相似度（key 精确命中时为 1.0；向量命中时为余弦相似度）。
        strict: 是否为高置信判重（True=key 精确或相似度 ≥ 判重阈值，可 MERGE；
            False=疑似冲突层命中（相似度介于判重与冲突阈值之间），仲裁结论
            为 MERGE 时降级 COEXIST，防止同主题不同侧面被误合并）。
    """

    memory: Memory
    similarity: float
    strict: bool = True


class ConflictAction(StrEnum):
    """LLM 冲突仲裁的动作结论。"""

    MERGE = "merge"  # 语义一致：合并进旧条目并提升 confidence
    SUPERSEDE = "supersede"  # 新事实成立：旧条目 superseded，新条目挂版本链
    COEXIST = "coexist"  # 无法判定：双条置 conflicted 待用户裁决


@dataclass(frozen=True)
class ConflictOutcome:
    """单次冲突仲裁结果。

    Attributes:
        action: 仲裁动作。
        merged_content: MERGE 时合并后的新正文；其余动作为 None。
        merged_confidence: MERGE 时提升后的置信度；其余动作为 None。
    """

    action: ConflictAction
    merged_content: str | None = None
    merged_confidence: float | None = None


@dataclass(frozen=True)
class ScoredMemory:
    """检索返回的单条记忆（带打分元数据，供 Context Builder 排版引用）。

    Attributes:
        memory: 命中的记忆条目。
        similarity: 与 query 的向量余弦相似度。
        score: 加权重排综合分（sim*0.5 + importance*0.2 + recency*0.15 + hit*0.15）。
    """

    memory: Memory
    similarity: float
    score: float
