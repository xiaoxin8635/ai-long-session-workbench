"""记忆模型（docs/01 §4.2：memories / memory_events）。

memories 为统一长期记忆表：memory_type 区分 episodic/semantic/procedural，
向量本体存向量库（vector_ref 引用），版本链通过 supersedes_id 串联。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import MemoryEventSource, MemoryEventType, MemoryStatus, MemoryType


class Memory(UUIDMixin, TimestampMixin, Base):
    """长期记忆（四层记忆中的三层落此表；working/knowledge 各有归属）。

    Attributes:
        key: 结构化主题键（如 skill.langgraph），冲突检测与去重依据。
        confidence: 抽取置信度（LLM 自评 0~1）。
        importance: 重要性评分（检索加权重排使用）。
        status: 状态机 active/superseded/conflicted/archived/deleted。
        supersedes_id: 指向被替代的旧记忆，构成版本链。
        vector_ref: 向量库中的引用 ID（隔离于业务库）。
        expires_at: TTL 到期时间；检索时过滤（软过期）。
        hit_count / last_hit_at: 命中统计（反哺 importance 校准）。
    """

    __tablename__ = "memories"
    __table_args__ = (
        Index("ix_memories_ws_type_status", "workspace_id", "memory_type", "status"),
        Index("ix_memories_ws_key", "workspace_id", "key"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    memory_type: Mapped[MemoryType] = mapped_column(String(16), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    vector_ref: Mapped[str | None] = mapped_column(String(128))

    confidence: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    importance: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    status: Mapped[MemoryStatus] = mapped_column(
        String(16), default=MemoryStatus.ACTIVE, nullable=False
    )

    source_session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    source_message_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)

    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hit_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryEvent(UUIDMixin, Base):
    """记忆事件流水：create/update/merge/conflict/supersede/hit/expire/
    edit_by_user/delete 全量留痕（审计与调试依据）。"""

    __tablename__ = "memory_events"
    __table_args__ = (Index("ix_memory_events_memory_created", "memory_id", "created_at"),)

    memory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("memories.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[MemoryEventType] = mapped_column(String(20), nullable=False)
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    source: Mapped[MemoryEventSource] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
