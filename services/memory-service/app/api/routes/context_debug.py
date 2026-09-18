"""Context 调试端点（M-05，docs/06 §7）：POST /api/context/preview。

只跑取数 + 装配（滚动摘要 / 记忆检索 / Working Memory 窗口 → 区块预算裁剪），
不调用 LLM、不落库；返回最终 messages 与各区块 token 用量，
用于验证预算 profile 效果与排查"记忆为何没注入"类问题。
"""

import uuid as uuid_mod

from fastapi import APIRouter

from app.context.budget import BudgetConfigError, load_budget
from app.context.builder import default_builder
from app.core.deps import CurrentUser, DbDep, get_redis, require_ws_member
from app.core.errors import AppError, NotFoundError
from app.memory.working_memory import WorkingMemory
from app.repositories import session_repo
from app.schemas.context import (
    ContextPreviewRequest,
    ContextPreviewResponse,
    ContextSectionUsage,
)

router = APIRouter(prefix="/api/context", tags=["context"])


@router.post(
    "/preview",
    summary="上下文装配预览（不调 LLM，返回 messages 与区块用量）",
    response_model=ContextPreviewResponse,
)
async def preview_context(
    payload: ContextPreviewRequest, user: CurrentUser, db: DbDep
) -> ContextPreviewResponse:
    """按给定 query/profile 装配上下文并返回明细。

    Raises:
        AppError: 422 workspace_id/session_id 非法；500 预算配置错误。
        PermissionDeniedError: 403 非成员。
        NotFoundError: 404 会话不存在。
    """
    ws_id = await require_ws_member(payload.workspace_id, user, db)
    try:
        session_id = uuid_mod.UUID(payload.session_id)
    except ValueError as exc:
        raise AppError("invalid_metadata", 422, "session_id 非法") from exc
    session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=session_id)
    if session is None:
        raise NotFoundError("会话不存在")

    try:
        budget = load_budget(payload.profile)
    except BudgetConfigError as exc:
        raise AppError("budget_config_error", 500, str(exc)) from exc

    builder = default_builder(WorkingMemory(get_redis()))
    try:
        assembled = await builder.build(
            db,
            ws_id=ws_id,
            user_id=user.id,
            session_id=session_id,
            query=payload.query,
            profile=payload.profile,
        )
    except BudgetConfigError as exc:  # 兜底：build 内部同源加载失败
        raise AppError("budget_config_error", 500, str(exc)) from exc

    return ContextPreviewResponse(
        profile=budget.profile,
        window_tokens=budget.window_tokens,
        available_tokens=budget.available_tokens,
        total_tokens=assembled.total_tokens,
        sections=[
            ContextSectionUsage(
                key=u.key.value,
                budget_tokens=u.budget_tokens,
                included=u.included,
                dropped=u.dropped,
                tokens=u.tokens,
            )
            for u in assembled.sections
        ],
        messages=assembled.messages,
    )
