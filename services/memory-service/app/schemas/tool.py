"""工具 API 模型（M-09，docs/01 §6.6）。

执行/确认/审计三类端点的请求响应体；ToolInfo.args_schema 直接采用
Pydantic 生成的 JSON Schema（与 MCP tool 定义同构，M3 可直出）。
"""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ToolCallStatus, ToolRiskLevel


class ToolInfo(BaseModel):
    """工具清单条目（GET /api/tools）。"""

    name: str
    description: str
    risk: ToolRiskLevel
    args_schema: dict[str, Any] = Field(description="参数 JSON Schema（MCP 同构）")


class ToolExecuteRequest(BaseModel):
    """执行工具请求体。"""

    tool: str = Field(min_length=1, max_length=128, description="工具名（见 GET /api/tools）")
    args: dict[str, Any] = Field(default_factory=dict, description="工具参数")
    session_id: uuid.UUID | None = Field(default=None, description="来源会话（审计挂链）")


class ToolExecutionRead(BaseModel):
    """执行工具响应体（202 pending / 200 终态）。"""

    call_id: str
    status: ToolCallStatus
    requires_confirmation: bool
    result: dict[str, Any] | None = None
    error: str | None = None


class ToolConfirmRequest(BaseModel):
    """确认 external 调用请求体。"""

    approve: bool = Field(description="True 执行 / False 拒绝")


class ToolConfirmRead(BaseModel):
    """确认响应体（终态由后台任务落库，可经审计端点查询）。"""

    call_id: str
    accepted: bool = Field(description="确认是否被接受（非 pending 态拒绝）")


class ToolCallRead(BaseModel):
    """工具调用审计条目（GET /api/tools/calls）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID | None
    tool_name: str
    risk_level: ToolRiskLevel
    args: dict[str, Any]
    status: ToolCallStatus
    approver_id: uuid.UUID | None
    result_digest: str | None
    created_at: datetime
