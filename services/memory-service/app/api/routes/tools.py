"""工具端点（M-09，docs/01 §6.6）。

  GET   /api/tools                       工具清单（名称/描述/schema/风险分级）
  POST  /api/tools/execute               执行（read_only/write 直行；external 202 待确认）
  POST  /api/tools/calls/{id}/confirm    确认/拒绝 external 调用
  GET   /api/tools/calls                 调用审计（最近在前）

鉴权：Bearer JWT + workspace 成员校验（CurrentWorkspaceQuery，403 防枚举）。
M1 口径：LLM 自动 tool-loop 属 M-08 M3（LangGraph 图），本端点供前端与
调试直调；权限分级/审计/确认流已是生产口径。
"""

import uuid as uuid_mod
from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.core.deps import CurrentUser, CurrentWorkspaceQuery, DbDep
from app.repositories import tool_call_repo
from app.schemas.tool import (
    ToolCallRead,
    ToolConfirmRead,
    ToolConfirmRequest,
    ToolExecuteRequest,
    ToolExecutionRead,
    ToolInfo,
)
from app.tools.builtin import doc_reader, todo, web_fetch  # noqa: F401 —— 注册副作用
from app.tools.registry import default_registry
from app.tools.router import confirm_tool_call, execute_tool

router = APIRouter(prefix="/api/tools", tags=["tools"])


@router.get("", response_model=list[ToolInfo], summary="工具清单")
async def list_tools() -> list[ToolInfo]:
    """全部已注册工具（含参数 schema 与风险分级）。"""
    return [
        ToolInfo(
            name=tool.name,
            description=tool.description,
            risk=tool.risk,
            args_schema=tool.args_model.model_json_schema() if tool.args_model else {},
        )
        for tool in default_registry.list()
    ]


@router.post(
    "/execute",
    response_model=ToolExecutionRead,
    status_code=202,
    summary="执行工具",
)
async def execute(
    ws: CurrentWorkspaceQuery,
    user: CurrentUser,
    db: DbDep,
    payload: ToolExecuteRequest,
) -> Response:
    """按风险分级执行：external 返回 202 待确认，其余 200 直接给终态。

    Raises:
        AppError: 404 工具未注册 / 422 参数不合法（RFC 7807）。
    """
    outcome = await execute_tool(
        db,
        ws_id=ws.id,
        user=user,
        name=payload.tool,
        raw_args=payload.args,
        session_id=payload.session_id,
    )
    body = ToolExecutionRead(
        call_id=str(outcome.call_id),
        status=outcome.status,
        requires_confirmation=outcome.requires_confirmation,
        result=outcome.result,
        error=outcome.error,
    )
    # external 等确认走 202；立即路径覆盖 status_code 为 200
    status_code = 202 if outcome.requires_confirmation else 200
    return Response(
        content=body.model_dump_json(exclude_none=True),
        media_type="application/json",
        status_code=status_code,
    )


@router.post("/calls/{call_id}/confirm", response_model=ToolConfirmRead, summary="确认工具调用")
async def confirm(
    call_id: uuid_mod.UUID,
    ws: CurrentWorkspaceQuery,
    user: CurrentUser,
    db: DbDep,
    payload: ToolConfirmRequest,
) -> ToolConfirmRead:
    """确认/拒绝一条 pending 的 external 调用（60s 未确认自动超时拒绝）。

    Raises:
        AppError: 404 记录不存在 / 409 已非 pending 终态。
    """
    await confirm_tool_call(db, ws_id=ws.id, user=user, call_id=call_id, approve=payload.approve)
    return ToolConfirmRead(call_id=str(call_id), accepted=True)


@router.get("/calls", response_model=list[ToolCallRead], summary="工具调用审计")
async def list_calls(
    ws: CurrentWorkspaceQuery,
    db: DbDep,
    session_id: Annotated[uuid_mod.UUID | None, Query(description="会话过滤")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[ToolCallRead]:
    """调用审计清单（created_at 倒序；含参数与结果摘要）。"""
    logs = await tool_call_repo.list_logs(db, ws_id=ws.id, session_id=session_id, limit=limit)
    return [ToolCallRead.model_validate(log) for log in logs]
