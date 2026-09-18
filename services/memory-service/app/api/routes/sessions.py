"""会话路由：CRUD / 消息历史 / 追加消息（docs/01 §6.3，docs/06 §M-03）。

workspace 归属经 query 参数 workspace_id 传递，由
CurrentWorkspaceQuery 依赖完成成员校验（隔离第二道防线）。
"""
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, CurrentWorkspaceQuery, DbDep
from app.core.errors import AppError
from app.models.enums import SessionStatus
from app.schemas.common import Page
from app.schemas.message import MessageRead
from app.schemas.session import (
    MessageCreate,
    SessionCreate,
    SessionDetail,
    SessionRead,
    SessionUpdate,
)
from app.services.session_service import SessionService

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_service = SessionService()

# 分页 query 参数（列表与消息历史共用约束）
LimitQuery = Annotated[int, Query(ge=1, le=100, description="页大小（1-100）")]
OffsetQuery = Annotated[int, Query(ge=0, description="偏移量")]


@router.get("", response_model=Page[SessionRead], summary="会话列表（分页、关键词）")
async def list_sessions(
    ws: CurrentWorkspaceQuery,
    db: DbDep,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
    q: Annotated[str | None, Query(max_length=100, description="标题关键词")] = None,
    include_archived: Annotated[bool, Query(description="是否含归档会话")] = False,
) -> Page[SessionRead]:
    """当前 workspace 的会话列表（软删除始终排除）。"""
    sessions, total = await _service.list_paginated(
        db,
        ws_id=ws.id,
        limit=limit,
        offset=offset,
        keyword=q,
        include_archived=include_archived,
    )
    return Page(items=sessions, total=total)


@router.post(
    "",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建会话",
)
async def create_session(
    payload: SessionCreate, ws: CurrentWorkspaceQuery, user: CurrentUser, db: DbDep
) -> SessionRead:
    """创建会话；title 为空时用占位（首条消息后由摘要命名）。"""
    return await _service.create(db, ws_id=ws.id, user=user, title=payload.title)


@router.get("/{session_id}", response_model=SessionDetail, summary="会话详情")
async def get_session(
    session_id: uuid.UUID, ws: CurrentWorkspaceQuery, db: DbDep
) -> SessionDetail:
    """会话详情（含首页消息；完整历史走 messages 子端点）。"""
    return await _service.get_detail(db, ws_id=ws.id, session_id=session_id)


@router.patch("/{session_id}", response_model=SessionRead, summary="重命名 / 归档 / 恢复")
async def update_session(
    session_id: uuid.UUID,
    payload: SessionUpdate,
    ws: CurrentWorkspaceQuery,
    db: DbDep,
) -> SessionRead:
    """按提交字段部分更新（title / status；删除是独立端点）。"""
    if payload.status is SessionStatus.DELETED:
        raise AppError("invalid_operation", 422, "删除请使用 DELETE 端点")
    if payload.title is not None:
        await _service.rename(db, ws_id=ws.id, session_id=session_id, title=payload.title)
    if payload.status is SessionStatus.ARCHIVED:
        await _service.archive(db, ws_id=ws.id, session_id=session_id)
    elif payload.status is SessionStatus.ACTIVE:
        await _service.restore(db, ws_id=ws.id, session_id=session_id)
    refreshed = await _service.get(db, ws_id=ws.id, session_id=session_id)
    return SessionRead.model_validate(refreshed)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除会话")
async def delete_session(
    session_id: uuid.UUID,
    ws: CurrentWorkspaceQuery,
    db: DbDep,
    cascade_memories: Annotated[
        bool, Query(description="true 时同步归档该会话来源的记忆")
    ] = False,
) -> None:
    """软删除；cascade_memories=True 时同步归档来源记忆。"""
    await _service.delete(
        db, ws_id=ws.id, session_id=session_id, cascade_memories=cascade_memories
    )


@router.get(
    "/{session_id}/messages",
    response_model=Page[MessageRead],
    summary="消息历史（时间正序分页）",
)
async def list_messages(
    session_id: uuid.UUID,
    ws: CurrentWorkspaceQuery,
    db: DbDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> Page[MessageRead]:
    """会话消息历史（含引用与 trace 链接的 metadata）。"""
    messages, total = await _service.list_messages(
        db, ws_id=ws.id, session_id=session_id, limit=limit, offset=offset
    )
    return Page(items=messages, total=total)


@router.post(
    "/{session_id}/messages",
    response_model=MessageRead,
    status_code=status.HTTP_201_CREATED,
    summary="追加消息（锁 + 乐观锁）",
)
async def append_message(
    session_id: uuid.UUID,
    payload: MessageCreate,
    ws: CurrentWorkspaceQuery,
    db: DbDep,
) -> MessageRead:
    """追加消息：token 计数、metadata 落库、版本冲突返回 409。"""
    return await _service.append_message(
        db, ws_id=ws.id, session_id=session_id, payload=payload
    )
