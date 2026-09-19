"""任务与待办端点（M-10，docs/01 §6.6）。

  GET    /api/tasks            列表（status 过滤，updated_at 倒序）
  POST   /api/tasks            创建（可选 query session_id 自动挂链）
  PATCH  /api/tasks/{task_id}  更新（PATCH 语义；状态变更同步进度记忆）

Agent（todo 工具）与前端共用同一 TaskService；鉴权：Bearer JWT +
workspace 成员校验（CurrentWorkspaceQuery，403 防枚举）。
"""

import uuid as uuid_mod
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.deps import CurrentUser, CurrentWorkspaceQuery, DbDep
from app.models.enums import TaskStatus
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.services.task_service import task_service

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("", response_model=list[TaskRead], summary="任务列表")
async def list_tasks(
    ws: CurrentWorkspaceQuery,
    db: DbDep,
    status: Annotated[TaskStatus | None, Query(description="状态过滤；缺省为全部")] = None,
) -> list[TaskRead]:
    """workspace 内任务清单（最近变动在前）。"""
    tasks = await task_service.list_tasks(db, ws_id=ws.id, status=status)
    return [TaskRead.model_validate(task) for task in tasks]


@router.post("", status_code=201, response_model=TaskRead, summary="创建任务")
async def create_task(
    ws: CurrentWorkspaceQuery,
    user: CurrentUser,
    db: DbDep,
    payload: TaskCreate,
    session_id: Annotated[
        uuid_mod.UUID | None, Query(description="来源会话（自动挂入关联列表）")
    ] = None,
) -> TaskRead:
    """创建任务并同步 job.*.progress 记忆（Task Continuity 起点）。"""
    task = await task_service.create(
        db, ws_id=ws.id, user_id=user.id, payload=payload, session_id=session_id
    )
    await db.commit()
    await db.refresh(task)
    return TaskRead.model_validate(task)


@router.patch("/{task_id}", response_model=TaskRead, summary="更新任务")
async def update_task(
    task_id: uuid_mod.UUID,
    ws: CurrentWorkspaceQuery,
    user: CurrentUser,
    db: DbDep,
    payload: TaskUpdate,
    session_id: Annotated[
        uuid_mod.UUID | None, Query(description="来源会话（自动挂入关联列表）")
    ] = None,
) -> TaskRead:
    """更新任务（仅提交字段生效；状态/标题变更触发记忆同步）。

    Raises:
        NotFoundError: 任务不存在（404）。
    """
    task = await task_service.update(
        db, ws_id=ws.id, task_id=task_id, user_id=user.id, payload=payload, session_id=session_id
    )
    await db.commit()
    await db.refresh(task)
    return TaskRead.model_validate(task)
