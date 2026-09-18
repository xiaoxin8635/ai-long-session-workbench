"""memories 新增 embedding 向量列 + HNSW 索引（M-04）。

Revision ID: 31dc4bbde4c9
Revises: c4e9a17d3f52
Create Date: 2026-09-18 21:52:50.507342

变更内容（docs/06 §6 抽取/检索管线）：
  - memories.embedding：pgvector VECTOR(1024)，存 bge-m3 稠密向量
    （可空 —— embedding 服务不可用时降级为无向量条目，仅走 key 判重）
  - HNSW 余弦索引：检索召回（search_candidates）与去重近邻（find_similar）共用

说明：autogenerate 曾误报 workspace_members 缺 uq_ws_member 唯一约束，
实际该列组合已是复合主键（唯一性由 PK 保证）；冗余声明已从 ORM 移除，
本迁移不对 workspace_members 做任何变更。
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "31dc4bbde4c9"
down_revision: Union[str, None] = "c4e9a17d3f52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """安装 pgvector 扩展 → 加列 → 建 HNSW 索引。"""
    # vector 扩展（pgvector/pgvector 镜像自带；幂等）。必须先于 vector 列创建。
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column("memories", sa.Column("embedding", Vector(dim=1024), nullable=True))
    # HNSW 余弦近邻索引（vector_cosine_ops 对应 cosine_distance 查询）
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memories_embedding_hnsw "
        "ON memories USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    """回滚：删索引 → 删列（扩展保留，其他表可能仍在使用）。"""
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.drop_column("memories", "embedding")
