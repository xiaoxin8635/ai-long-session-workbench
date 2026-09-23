"""add mcp_servers table

Revision ID: c7335478c008
Revises: 5de8d97c8e0d
Create Date: 2026-09-23 12:18:53.457334

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c7335478c008'
down_revision: Union[str, None] = '5de8d97c8e0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # HNSW 索引（ix_memories_embedding_hnsw / ix_knowledge_chunks_embedding_hnsw）
    # 由历史迁移的裸 SQL 创建、不在 ORM metadata 中，autogenerate 误判为待删除，
    # 已人工剔除——本迁移只新增 mcp_servers 表
    op.create_table('mcp_servers',
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('transport', sa.String(length=8), nullable=False),
    sa.Column('url', sa.String(length=2048), nullable=True),
    sa.Column('command', sa.String(length=512), nullable=True),
    sa.Column('args', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('env', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )


def downgrade() -> None:
    op.drop_table('mcp_servers')
