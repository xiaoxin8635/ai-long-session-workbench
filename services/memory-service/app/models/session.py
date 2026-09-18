"""会话与消息模型（docs/01 §4.1：sessions / messages）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import MessageRole, SessionStatus


class Session(UUIDMixin, TimestampMixin, Base):
    """会话：多会话管理与滚动摘要的载体。

    Attributes:
        rolling_summary: 滚动摘要（M-06 写入；当前会话上下文的一部分）。
        token_total: 会话累计 token（成本统计口径之一）。
        version: 乐观锁版本（并发追加消息时条件更新，冲突方 409）。
    """

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_ws_updated", "workspace_id", "updated_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(200), default="新会话", nullable=False)
    status: Mapped[SessionStatus] = mapped_column(
        String(16), default=SessionStatus.ACTIVE, nullable=False
    )
    rolling_summary: Mapped[str | None] = mapped_column(Text)
    token_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class Message(UUIDMixin, Base):
    """消息：append-only；metadata 存引用/工具调用/trace 链接。

    注意：属性名用 meta（避开 SQLAlchemy 保留属性 metadata），
    数据库列名保持 "metadata" 与 docs/01 表设计一致。
    """

    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_session_created", "session_id", "created_at"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[MessageRole] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
