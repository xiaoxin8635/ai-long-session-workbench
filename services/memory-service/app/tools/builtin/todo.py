"""待办工具族（M-09，write 风险）：todo.create / todo.list / todo.update / todo.complete。

DoD 场景：「把字节面试准备加到待办」→ todo.create 写入 tasks 表且
tool_call_logs 审计可查。全部动作经 TaskService 编排，与 REST /api/tasks
同一条记忆同步链路（job.*.progress）。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import TaskStatus, ToolRiskLevel
from app.models.user import User
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.services.task_service import task_service
from app.tools.registry import ToolDefinition, ToolRegistry, default_registry


class TodoCreateArgs(BaseModel):
    """todo.create 参数。"""

    title: str = Field(min_length=1, max_length=255, description="任务标题")
    priority: int = Field(default=0, ge=0, le=1, description="0 普通 / 1 高")
    due_date: datetime | None = Field(default=None, description="截止时间（ISO 8601）")


class TodoListArgs(BaseModel):
    """todo.list 参数。"""

    status: TaskStatus | None = Field(default=None, description="状态过滤；缺省全部")


class TodoUpdateArgs(BaseModel):
    """todo.update 参数（仅提交字段生效）。"""

    task_id: uuid.UUID = Field(description="任务 ID")
    title: str | None = Field(default=None, min_length=1, max_length=255)
    status: TaskStatus | None = None
    priority: int | None = Field(default=None, ge=0, le=1)
    due_date: datetime | None = Field(default=None, description="提交 null 清除截止时间")


class TodoCompleteArgs(BaseModel):
    """todo.complete 参数。"""

    task_id: uuid.UUID = Field(description="任务 ID")


def _dump(task: object) -> dict:
    """Task ORM → JSON dict（handler 统一返回纯数据）。"""
    return TaskRead.model_validate(task).model_dump(mode="json")


async def _create(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    session_id: uuid.UUID | None,
    args: TodoCreateArgs,
) -> dict:
    """创建任务（自动挂当前会话）。"""
    task = await task_service.create(
        db,
        ws_id=ws_id,
        user_id=user.id,
        payload=TaskCreate(**args.model_dump()),
        session_id=session_id,
    )
    await db.commit()
    await db.refresh(task)
    return {"task": _dump(task)}


async def _list(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    session_id: uuid.UUID | None,
    args: TodoListArgs,
) -> dict:
    """列任务（最近变动在前）。"""
    tasks = await task_service.list_tasks(db, ws_id=ws_id, status=args.status)
    return {"tasks": [_dump(task) for task in tasks]}


async def _update(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    session_id: uuid.UUID | None,
    args: TodoUpdateArgs,
) -> dict:
    """更新任务（PATCH 语义；状态/标题变更触发记忆同步）。"""
    task = await task_service.update(
        db,
        ws_id=ws_id,
        task_id=args.task_id,
        user_id=user.id,
        payload=TaskUpdate(**args.model_dump(exclude={"task_id"})),
        session_id=session_id,
    )
    await db.commit()
    await db.refresh(task)
    return {"task": _dump(task)}


async def _complete(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user: User,
    session_id: uuid.UUID | None,
    args: TodoCompleteArgs,
) -> dict:
    """完结任务（todo.update 的便捷封装）。"""
    task = await task_service.update(
        db,
        ws_id=ws_id,
        task_id=args.task_id,
        user_id=user.id,
        payload=TaskUpdate(status=TaskStatus.DONE),
        session_id=session_id,
    )
    await db.commit()
    await db.refresh(task)
    return {"task": _dump(task)}


def register(registry: ToolRegistry) -> None:
    """向注册表登记待办工具族（builtin 包导入时调用）。

    Args:
        registry: 目标注册表。
    """
    registry.register(
        ToolDefinition(
            name="todo.create",
            description="为用户创建一条待办任务（自动挂当前会话，写入长期任务清单）",
            risk=ToolRiskLevel.WRITE,
            args_model=TodoCreateArgs,
            handler=_create,
        )
    )
    registry.register(
        ToolDefinition(
            name="todo.list",
            description="列出用户的待办任务（可按状态过滤，最近变动在前）",
            risk=ToolRiskLevel.WRITE,
            args_model=TodoListArgs,
            handler=_list,
        )
    )
    registry.register(
        ToolDefinition(
            name="todo.update",
            description="更新待办任务的标题/状态/优先级/截止时间（仅提交字段生效）",
            risk=ToolRiskLevel.WRITE,
            args_model=TodoUpdateArgs,
            handler=_update,
        )
    )
    registry.register(
        ToolDefinition(
            name="todo.complete",
            description="把待办任务标记为已完成",
            risk=ToolRiskLevel.WRITE,
            args_model=TodoCompleteArgs,
            handler=_complete,
        )
    )


register(default_registry)
