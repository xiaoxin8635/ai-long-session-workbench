"""任务/待办仓储（M-10，docs/01 §5.7 / docs/06 §12）。

Task Continuity 数据底座：Agent（经 todo 工具）与前端（REST）共用同一张
tasks 表，本仓储只做查询与构造，状态变更的记忆同步在 TaskService 层编排。
"""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TaskStatus
from app.models.task import Task

# 仍未完结的状态（开场简报与记忆 TTL 的过滤基准）
_UNFINISHED = (TaskStatus.OPEN, TaskStatus.DOING)


async def create_task(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    title: str,
    priority: int,
    due_date: datetime | None,
    related_session_ids: list[uuid.UUID],
) -> Task:
    """创建任务。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace（隔离边界）。
        title: 任务标题（1-255 字符）。
        priority: 0 普通 / 1 高（高优先任务开场注入）。
        due_date: 截止时间（可空）。
        related_session_ids: 关联会话列表（溯源与上下文注入用）。

    Returns:
        落库后的 Task 对象。
    """
    task = Task(
        workspace_id=ws_id,
        title=title,
        priority=priority,
        due_date=due_date,
        related_session_ids=[str(sid) for sid in related_session_ids],
    )
    db.add(task)
    await db.flush()
    return task


async def get_task(db: AsyncSession, *, ws_id: uuid.UUID, task_id: uuid.UUID) -> Task | None:
    """按 workspace + ID 取任务（隔离第一道防线）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        task_id: 任务 ID。

    Returns:
        命中的任务；无则 None。
    """
    result = await db.execute(select(Task).where(Task.workspace_id == ws_id, Task.id == task_id))
    return result.scalar_one_or_none()


async def list_tasks(
    db: AsyncSession, *, ws_id: uuid.UUID, status: TaskStatus | None
) -> list[Task]:
    """列任务（updated_at 倒序，最近变动在前）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        status: 状态过滤；None 为全部。

    Returns:
        任务列表。
    """
    stmt = select(Task).where(Task.workspace_id == ws_id)
    if status is not None:
        stmt = stmt.where(Task.status == status)
    stmt = stmt.order_by(Task.updated_at.desc())
    return list((await db.execute(stmt)).scalars().all())


async def unfinished_brief(db: AsyncSession, *, ws_id: uuid.UUID, limit: int) -> list[Task]:
    """开场简报：未完成任务按优先级降序 + 最近变动在前。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        limit: 条数上限。

    Returns:
        未完成任务列表（高优先在前）。
    """
    stmt = (
        select(Task)
        .where(Task.workspace_id == ws_id, Task.status.in_(_UNFINISHED))
        .order_by(Task.priority.desc(), Task.updated_at.desc())
        .limit(limit)
    )
    return list((await db.execute(stmt)).scalars().all())
