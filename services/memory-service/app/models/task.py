"""任务/待办模型（docs/01 §4.1：tasks，Task Continuity 数据底座）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import TaskStatus


class Task(UUIDMixin, TimestampMixin, Base):
    """任务：Agent（经 MCP 待办工具）与前端共用的实体。

    Attributes:
        related_session_ids: 关联会话列表（JSONB 数组），溯源与上下文注入用。
        priority: 0 普通 / 1 高（高优先任务在新会话开场注入）。
    """

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_ws_status", "workspace_id", "status"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[TaskStatus] = mapped_column(String(16), default=TaskStatus.OPEN, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    due_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    related_session_ids: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
