"""用户与工作区模型（docs/01 §4.1：users / workspaces / workspace_members）。

隔离设计：所有业务表带 workspace_id 外键（第一道防线）；
成员资格校验在服务层（第二道）；向量库按 workspace 分区（第三道）。
"""

import uuid

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDMixin
from app.models.enums import MemberRole


class User(UUIDMixin, TimestampMixin, Base):
    """用户表（认证主体；角色按 workspace 维度挂在成员表）。"""

    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:
        return f"<User {self.username}>"


class Workspace(UUIDMixin, TimestampMixin, Base):
    """工作区（数据隔离边界；注册时自动创建同名个人区）。"""

    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    settings: Mapped[dict] = mapped_column(JSONB, default=dict, nullable=False)

    def __repr__(self) -> str:
        return f"<Workspace {self.name}>"


class WorkspaceMember(Base, TimestampMixin):
    """用户-工作区多对多关系与角色（复合主键）。"""

    __tablename__ = "workspace_members"

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    role: Mapped[MemberRole] = mapped_column(String(16), default=MemberRole.MEMBER, nullable=False)
