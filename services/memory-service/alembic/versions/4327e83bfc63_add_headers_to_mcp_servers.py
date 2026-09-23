"""add headers to mcp_servers

Revision ID: 4327e83bfc63
Revises: c7335478c008
Create Date: 2026-09-23 15:08:44.545673

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4327e83bfc63'
down_revision: Union[str, None] = 'c7335478c008'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 远程 MCP 端点鉴权头（百炼/高德等托管服务需 Authorization 等自定义头）
    op.add_column(
        'mcp_servers',
        sa.Column('headers', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('mcp_servers', 'headers')
