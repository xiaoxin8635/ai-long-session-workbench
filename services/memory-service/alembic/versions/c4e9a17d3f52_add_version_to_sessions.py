"""add version column to sessions (M-03 optimistic lock)

Revision ID: c4e9a17d3f52
Revises: 91b7b99eadd8
Create Date: 2026-09-18 17:05:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4e9a17d3f52"
down_revision: Union[str, None] = "91b7b99eadd8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """sessions 增加 version 列（乐观锁，M-03 并发追加消息）。"""
    op.add_column(
        "sessions",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    """回滚：删除 version 列。"""
    op.drop_column("sessions", "version")
