"""workspace 相关 schema（docs/06 §M-02）。"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import MemberRole


class WorkspaceRead(BaseModel):
    """workspace 响应。"""

    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    created_at: datetime

    model_config = {"from_attributes": True}


class WorkspaceCreate(BaseModel):
    """创建 workspace 请求（后续协作场景用）。"""

    name: str = Field(min_length=1, max_length=128)


class MemberAdd(BaseModel):
    """添加成员请求。"""

    username: str = Field(min_length=1, max_length=64)
    role: MemberRole = Field(default=MemberRole.MEMBER)


class MemberRead(BaseModel):
    """成员响应。"""

    workspace_id: uuid.UUID
    user_id: uuid.UUID
    username: str
    display_name: str | None
    role: MemberRole
    created_at: datetime
