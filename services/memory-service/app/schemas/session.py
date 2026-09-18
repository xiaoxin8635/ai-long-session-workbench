"""会话相关 schema（docs/06 §M-03）。"""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import SessionStatus
from app.schemas.message import MessageRead


class SessionCreate(BaseModel):
    """创建会话请求。"""

    title: str | None = Field(default=None, max_length=200)


class SessionUpdate(BaseModel):
    """更新会话请求（PATCH：重命名 / 归档 / 恢复，字段可选）。"""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    status: SessionStatus | None = None


class SessionRead(BaseModel):
    """会话响应（列表/详情共用）。"""

    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    title: str
    status: SessionStatus
    token_total: int
    last_message_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class SessionDetail(SessionRead):
    """会话详情：附带首页消息（分页由 messages 子端点承担）。"""

    messages: list[MessageRead] = Field(default_factory=list)


class MessageCreate(BaseModel):
    """追加消息请求。

    Attributes:
        expected_version: 客户端持有的会话版本（乐观锁；不匹配返回 409），
            省略时仅依赖 Redis 会话锁串行化。
    """

    role: str = Field(pattern="^(user|assistant|tool|system)$")
    content: str = Field(min_length=1)
    metadata: dict = Field(default_factory=dict)
    expected_version: int | None = Field(default=None, ge=1)
