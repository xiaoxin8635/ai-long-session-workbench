"""SQLAlchemy 声明基类与公共 Mixin。

所有 ORM 模型继承 Base；业务表通过 UUIDMixin/TimestampMixin
获得统一主键与时间戳列（docs/01 §4）。

重要：本文件必须 import 全部模型模块，alembic env.py 以此为
metadata 来源（autogenerate 才能看到完整表结构）。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """全局声明基类。"""


class UUIDMixin:
    """UUID 主键（数据库端生成，避免应用侧生成造成的索引热点）。"""

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=func.gen_random_uuid(),  # PG13+ 内置；启用 pgcrypto 不需要
    )


class TimestampMixin:
    """created_at / updated_at 双时间戳（数据库端维护）。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ---- 集中导入全部模型（供 alembic autogenerate 使用，勿删）----
# 注意：Workspace/WorkspaceMember 定义在 user.py（无独立 workspace.py 模块）
from app.models import (  # noqa: E402,F401
    knowledge,
    memory,
    observability,
    session,
    task,
    user,
)
