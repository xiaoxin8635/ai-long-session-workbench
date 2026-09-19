"""知识库模型（docs/01 §4.1：knowledge_files / knowledge_chunks）。

向量与 BM25 分词随 chunk 行存储（pgvector 列 + tokens JSONB，M-07）：
workspace_id 为冗余隔离列（向量近邻检索免 join knowledge_files，
HNSW 索引扫描 + workspace 过滤可直接下推）。
"""

import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import KnowledgeFileStatus
from app.models.memory import EMBEDDING_DIM


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
    """文档切片：检索与引用定位的最小单元。

    Attributes:
        workspace_id: 冗余隔离列（= file.workspace_id；向量检索免 join）。
        embedding: content 的稠密向量（embedding 服务不可用置 NULL，
            检索自动排除；旧版本下线时批量置 NULL 实现"向量下线"）。
        tokens: BM25 预分词（jieba，ingest 时算好；检索时与 query 分词对齐）。
    """

    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        Index("ix_knowledge_chunks_file", "file_id", "chunk_index"),
        Index("ix_knowledge_chunks_ws", "workspace_id"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_files.id", ondelete="CASCADE"),
        nullable=False,
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict, nullable=False)
    # content 的稠密向量与 BM25 分词（M-07 混合检索双路数据）
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    tokens: Mapped[list] = mapped_column(JSONB, default=list, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
