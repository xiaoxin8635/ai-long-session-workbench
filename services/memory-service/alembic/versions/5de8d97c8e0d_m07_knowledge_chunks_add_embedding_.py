"""m07 knowledge chunks add embedding tokens and workspace index

M-07 RAG 知识库：knowledge_chunks 增加向量列（pgvector）、BM25 分词列
（JSONB）与 workspace 冗余隔离列（含 FK 与索引），并为向量列建 HNSW
余弦索引（手写 SQL，沿用 31dc4bbde4c9 模式；模型 __table_args__ 不含
该索引，autogenerate 误报的 memories HNSW 删除已人工剔除）。

Revision ID: 5de8d97c8e0d
Revises: 31dc4bbde4c9
Create Date: 2026-09-19 12:34:31.745619

"""

from typing import Sequence, Union

import pgvector.sqlalchemy.vector
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '5de8d97c8e0d'
down_revision: Union[str, None] = '31dc4bbde4c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # NOT NULL 列均无存量数据风险（M-07 前表为空）
    op.add_column('knowledge_chunks', sa.Column('workspace_id', sa.UUID(), nullable=False))
    op.add_column('knowledge_chunks', sa.Column('embedding', pgvector.sqlalchemy.vector.VECTOR(dim=1024), nullable=True))
    op.add_column('knowledge_chunks', sa.Column('tokens', postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default='[]'))
    op.create_index('ix_knowledge_chunks_ws', 'knowledge_chunks', ['workspace_id'], unique=False)
    op.create_foreign_key('fk_knowledge_chunks_workspace', 'knowledge_chunks', 'workspaces', ['workspace_id'], ['id'])
    # HNSW 余弦近邻索引（向量召回路；手写 SQL 因 SQLAlchemy 模型不表达该索引）
    op.execute(
        "CREATE INDEX ix_knowledge_chunks_embedding_hnsw ON knowledge_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_chunks_embedding_hnsw")
    op.drop_constraint('fk_knowledge_chunks_workspace', 'knowledge_chunks', type_='foreignkey')
    op.drop_index('ix_knowledge_chunks_ws', table_name='knowledge_chunks')
    op.drop_column('knowledge_chunks', 'tokens')
    op.drop_column('knowledge_chunks', 'embedding')
    op.drop_column('knowledge_chunks', 'workspace_id')
