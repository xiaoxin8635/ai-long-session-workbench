"""记忆管理端点（M-11，docs/01 §7 / docs/05 §7.1 面板后端）。

  - GET    /api/memories            列表（type/status/关键词过滤 + 分页，conflicted 置顶）
  - POST   /api/memories/search     检索测试（必须注册在 /{memory_id} 之前，
                                    否则 "search" 会被路径参数当 UUID 解析为 422）
  - GET    /api/memories/{id}       详情（版本链 + 事件流水）
  - PATCH  /api/memories/{id}       用户编辑（edit_by_user 审计）
  - DELETE /api/memories/{id}       软删除（delete 审计）
  - POST   /api/memories/{id}/resolve 冲突裁决（选择保留版本）

鉴权：Bearer JWT + workspace 成员校验（CurrentWorkspaceQuery，403 防枚举）。
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.core.deps import CurrentUser, CurrentWorkspaceQuery, DbDep
from app.models.enums import MemoryStatus, MemoryType
from app.schemas.common import Page
from app.schemas.memory import (
    MemoryDetail,
    MemoryRead,
    MemoryResolveRequest,
    MemorySearchHit,
    MemorySearchRequest,
    MemoryUpdate,
)
from app.services.memory_admin_service import MemoryAdminService

router = APIRouter(prefix="/api/memories", tags=["memories"])

_service = MemoryAdminService()

LimitQuery = Annotated[int, Query(ge=1, le=100, description="页大小（1-100）")]
OffsetQuery = Annotated[int, Query(ge=0, description="偏移量")]


@router.get("", response_model=Page[MemoryRead], summary="记忆列表（过滤/分页）")
async def list_memories(
    ws: CurrentWorkspaceQuery,
    db: DbDep,
    memory_type: Annotated[MemoryType | None, Query(description="类型过滤")] = None,
    memory_status: Annotated[
        MemoryStatus | None, Query(alias="status", description="状态过滤（软删除恒排除）")
    ] = None,
    q: Annotated[str | None, Query(max_length=100, description="key/content 关键词")] = None,
    limit: LimitQuery = 20,
    offset: OffsetQuery = 0,
) -> Page[MemoryRead]:
    """记忆面板主查询：conflicted 置顶 + updated_at 倒序。"""
    items, total = await _service.list_paginated(
        db,
        ws_id=ws.id,
        memory_type=memory_type,
        status=memory_status,
        keyword=q,
        limit=limit,
        offset=offset,
    )
    return Page(items=[MemoryRead.model_validate(m) for m in items], total=total)


@router.post(
    "/search",
    response_model=list[MemorySearchHit],
    summary="检索测试（与对话链路同检索器）",
)
async def search_memories(
    payload: MemorySearchRequest, ws: CurrentWorkspaceQuery, user: CurrentUser, db: DbDep
) -> list[MemorySearchHit]:
    """调试用：验证某 query 能召回哪些记忆（含综合分与相似度）。"""
    hits = await _service.search(db, ws_id=ws.id, user_id=user.id, payload=payload)
    return [
        MemorySearchHit(
            id=s.memory.id,
            memory_type=s.memory.memory_type,
            key=s.memory.key,
            content=s.memory.content,
            confidence=s.memory.confidence,
            importance=s.memory.importance,
            score=s.score,
            similarity=s.similarity,
        )
        for s in hits
    ]


@router.get("/{memory_id}", response_model=MemoryDetail, summary="记忆详情（版本链+事件）")
async def get_memory(memory_id: uuid.UUID, ws: CurrentWorkspaceQuery, db: DbDep) -> MemoryDetail:
    """详情含被替代版本链（时间倒序）与最近 20 条事件流水。"""
    memory, chain, events = await _service.get_detail(db, ws_id=ws.id, memory_id=memory_id)
    return MemoryDetail(
        **MemoryRead.model_validate(memory).model_dump(),
        version_chain=[MemoryRead.model_validate(m) for m in chain],
        events=events,
    )


@router.patch("/{memory_id}", response_model=MemoryRead, summary="用户编辑记忆")
async def update_memory(
    payload: MemoryUpdate, memory_id: uuid.UUID, ws: CurrentWorkspaceQuery, db: DbDep
) -> MemoryRead:
    """按提交字段部分更新（version+1，edit_by_user 审计；向量尽力刷新）。"""
    memory = await _service.update(db, ws_id=ws.id, memory_id=memory_id, payload=payload)
    return MemoryRead.model_validate(memory)


@router.delete("/{memory_id}", status_code=status.HTTP_204_NO_CONTENT, summary="软删除记忆")
async def delete_memory(memory_id: uuid.UUID, ws: CurrentWorkspaceQuery, db: DbDep) -> None:
    """status=deleted（列表与检索不再可见），事件流水留痕。"""
    await _service.delete(db, ws_id=ws.id, memory_id=memory_id)


@router.post("/{memory_id}/resolve", response_model=MemoryRead, summary="冲突裁决")
async def resolve_memory(
    payload: MemoryResolveRequest, memory_id: uuid.UUID, ws: CurrentWorkspaceQuery, db: DbDep
) -> MemoryRead:
    """保留本条（this）或对手方（other）；另一条 superseded 并挂版本链。"""
    winner = await _service.resolve(db, ws_id=ws.id, memory_id=memory_id, keep=payload.keep)
    return MemoryRead.model_validate(winner)
