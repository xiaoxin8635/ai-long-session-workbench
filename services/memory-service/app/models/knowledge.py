"""知识库模型（docs/01 §4.1：knowledge_files / knowledge_chunks）。

向量本体存向量库（按 workspace 隔离 partition），
本表只存结构与文本，chunk 与向量以 chunk.id 关联。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import KnowledgeFileStatus


class KnowledgeFile(UUIDMixin, TimestampMixin, Base):
    """知识库文件：上传 → 异步摄取（状态机）→ 版本管理。

    Attributes:
        checksum: 内容摘要（SHA-256），重复上传去重依据。
        version: 同名重传递增；旧版本向量下线不删除。
    """

    __tablename__ = "knowledge_files"
    __table_args__ = (Index("ix_knowledge_files_ws", "workspace_id", "status"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(32), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[KnowledgeFileStatus] = mapped_column(
        String(16), default=KnowledgeFileStatus.PARSING, nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )


class KnowledgeChunk(UUIDMixin, Base):
    """文档切片：检索与引用定位的最小单元。"""

    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_knowledge_chunks_file", "file_id", "chunk_index"),)

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
