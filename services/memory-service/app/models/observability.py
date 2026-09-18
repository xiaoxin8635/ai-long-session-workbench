"""观测与审计模型（docs/01 §4.1：tool_call_logs / token_usages / checkpoints）。"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDMixin
from app.models.enums import ToolCallStatus, ToolRiskLevel


class ToolCallLog(UUIDMixin, Base):
    """工具调用审计：全量参数与结果摘要（docs/01 §5.6）。"""

    __tablename__ = "tool_call_logs"
    __table_args__ = (Index("ix_tool_calls_session_created", "session_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    risk_level: Mapped[ToolRiskLevel] = mapped_column(String(16), nullable=False)
    args: Mapped[dict] = mapped_column(JSONB, nullable=False)
    result_digest: Mapped[str | None] = mapped_column(Text)  # 结果摘要（不存全量）
    status: Mapped[ToolCallStatus] = mapped_column(String(16), nullable=False)
    approver_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))  # external 风险确认人
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TokenUsage(UUIDMixin, Base):
    """每轮 token 用量：按区块拆分，支撑成本看板与 A/B 对比（docs/01 §5.3/§8）。"""

    __tablename__ = "token_usages"
    __table_args__ = (
        Index("ix_token_usages_session_turn", "session_id", "turn_no"),
        Index("ix_token_usages_ws_created", "workspace_id", "created_at"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    turn_no: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    memory_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    rag_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tool_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 6), default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Checkpoint(UUIDMixin, Base):
    """LangGraph 会话快照：中断任务恢复（docs/01 §5.1）。"""

    __tablename__ = "checkpoints"
    __table_args__ = (Index("ix_checkpoints_session_created", "session_id", "created_at"),)

    session_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
