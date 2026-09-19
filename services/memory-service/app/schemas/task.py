"""任务/待办 API 模型（M-10，docs/01 §6.6）。

Agent（todo 工具）与前端（REST）共用：字段口径与 tasks 表一一对应，
related_session_ids 服务端自动维护（创建/更新时追加当前会话）。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import TaskStatus


class TaskCreate(BaseModel):
    """创建任务请求体。"""

    title: str = Field(min_length=1, max_length=255, description="任务标题")
    priority: int = Field(default=0, ge=0, le=1, description="0 普通 / 1 高（开场注入）")
    due_date: datetime | None = Field(default=None, description="截止时间（可空）")


class TaskUpdate(BaseModel):
    """更新任务请求体（PATCH 语义：仅提交的字段生效）。

    due_date 提交 null 表示清除截止时间（与未提交区分，靠 model_fields_set）。
    """

    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: TaskStatus | None = None
    priority: int | None = Field(default=None, ge=0, le=1)
    due_date: datetime | None = None


class TaskRead(BaseModel):
    """任务响应体。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    status: TaskStatus
    priority: int
    due_date: datetime | None
    related_session_ids: list[str]
    created_at: datetime
    updated_at: datetime
