"""消息相关 schema（docs/06 §M-03）。"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class MessageRead(BaseModel):
    """消息响应（append-only，无更新端点）。"""

    id: uuid.UUID
    session_id: uuid.UUID
    role: str
    content: str
    token_count: int
    version: int
    metadata: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}

    # ORM 属性名为 meta（避开 SQLAlchemy 保留属性），映射到 schema 的 metadata
    @classmethod
    def from_message(cls, message: object) -> "MessageRead":
        """从 ORM Message 构造（meta → metadata 字段名转换）。"""
        return cls(
            id=message.id,
            session_id=message.session_id,
            role=message.role,
            content=message.content,
            token_count=message.token_count,
            version=message.version,
            metadata=message.meta,
            created_at=message.created_at,
        )
