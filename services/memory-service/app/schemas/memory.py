"""记忆管理 API 的请求/响应模型（M-11，docs/01 §7 / docs/05 §7.1 面板后端）。"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import MemoryEventSource, MemoryEventType, MemoryStatus, MemoryType


class MemoryRead(BaseModel):
    """记忆条目视图（列表与详情共用基础字段）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    memory_type: MemoryType
    key: str
    content: str
    confidence: float
    importance: float
    status: MemoryStatus
    version: int
    source_session_id: uuid.UUID | None
    source_message_ids: list[str]
    supersedes_id: uuid.UUID | None
    expires_at: datetime | None
    hit_count: int
    last_hit_at: datetime | None
    created_at: datetime
    updated_at: datetime


class MemoryEventRead(BaseModel):
    """记忆事件流水视图（审计与溯源）。"""

    model_config = ConfigDict(from_attributes=True)

    event_type: MemoryEventType
    old_value: str | None
    new_value: str | None
    source: MemoryEventSource
    created_at: datetime


class MemoryDetail(MemoryRead):
    """记忆详情：基础字段 + 版本链（被替代历史）+ 事件流水。"""

    version_chain: list[MemoryRead] = Field(default_factory=list)
    events: list[MemoryEventRead] = Field(default_factory=list)


class MemoryUpdate(BaseModel):
    """用户编辑请求（按提交字段部分更新；expires_at 传 null 表示清除 TTL）。"""

    content: str | None = Field(default=None, min_length=1, max_length=8000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    expires_at: datetime | None = None


class MemoryResolveRequest(BaseModel):
    """冲突裁决请求：保留路径指向的条目（this）或同 key 的另一条（other）。"""

    keep: Literal["this", "other"]


class MemorySearchRequest(BaseModel):
    """检索调试请求（记忆面板"搜索测试"）。"""

    query: str = Field(min_length=1, max_length=500)
    top_k: int = Field(default=10, ge=1, le=50)


class MemorySearchHit(BaseModel):
    """检索命中条目（面板展示用，含综合分与相似度）。"""

    id: uuid.UUID
    memory_type: MemoryType
    key: str
    content: str
    confidence: float
    importance: float
    score: float
    similarity: float
