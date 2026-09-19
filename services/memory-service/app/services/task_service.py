"""任务服务（M-10，docs/01 §5.7 / docs/06 §12）。

Task Continuity 编排层：
  - CRUD（REST /api/tasks 与 todo 工具共用）
  - sync_to_memory：状态/标题变更同步为 semantic 记忆（job.<task_id>.progress，
    SYSTEM source、TTL 顺延、向量尽力刷新），双向一致
  - active_brief：未完成任务简报（高优先在前），供 ContextBuilder 开场注入
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFoundError
from app.llm.embeddings import EmbeddingError, get_embedding_client
from app.models.enums import TaskStatus
from app.models.memory import Memory
from app.models.task import Task
from app.repositories import memory_repo, task_repo
from app.schemas.task import TaskCreate, TaskUpdate

logger = logging.getLogger(__name__)


class TaskService:
    """任务与待办的领域服务（无状态，方法级编排）。"""

    async def create(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: TaskCreate,
        session_id: uuid.UUID | None = None,
    ) -> Task:
        """创建任务并同步进度记忆。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            user_id: 操作者（进度记忆归属用户）。
            payload: 创建参数。
            session_id: 来源会话（自动挂入 related_session_ids）。

        Returns:
            落库后的 Task。
        """
        related = [session_id] if session_id is not None else []
        task = await task_repo.create_task(
            db,
            ws_id=ws_id,
            title=payload.title,
            priority=payload.priority,
            due_date=payload.due_date,
            related_session_ids=related,
        )
        await self.sync_to_memory(db, task=task, user_id=user_id)
        return task

    async def update(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: TaskUpdate,
        session_id: uuid.UUID | None = None,
    ) -> Task:
        """更新任务（PATCH 语义）并在状态/标题变化时同步记忆。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            task_id: 任务 ID。
            user_id: 操作者（进度记忆归属用户）。
            payload: 更新字段（仅提交项生效）。
            session_id: 来源会话（追加挂链，去重）。

        Returns:
            更新后的 Task。

        Raises:
            NotFoundError: 任务不存在（404）。
        """
        task = await task_repo.get_task(db, ws_id=ws_id, task_id=task_id)
        if task is None:
            raise NotFoundError("任务")
        fields = payload.model_fields_set
        old_status = task.status
        changed = False
        if "title" in fields and payload.title is not None and payload.title != task.title:
            task.title = payload.title
            changed = True
        if "status" in fields and payload.status is not None and payload.status != task.status:
            task.status = payload.status
            changed = True
        if "priority" in fields and payload.priority is not None:
            task.priority = payload.priority
            changed = True
        # due_date：提交 null 清除、提交值覆盖、未提交不动（model_fields_set 区分）
        if "due_date" in fields and payload.due_date != task.due_date:
            task.due_date = payload.due_date
            changed = True
        if session_id is not None:
            sid = str(session_id)
            if sid not in (task.related_session_ids or []):
                task.related_session_ids = [*(task.related_session_ids or []), sid]
        if changed:
            await db.flush()
            await self.sync_to_memory(db, task=task, user_id=user_id, old_status=old_status)
        return task

    async def list_tasks(
        self, db: AsyncSession, *, ws_id: uuid.UUID, status: TaskStatus | None
    ) -> list[Task]:
        """列任务（updated_at 倒序）。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            status: 状态过滤；None 为全部。

        Returns:
            任务列表。
        """
        return await task_repo.list_tasks(db, ws_id=ws_id, status=status)

    async def sync_to_memory(
        self,
        db: AsyncSession,
        *,
        task: Task,
        user_id: uuid.UUID,
        old_status: TaskStatus | None = None,
    ) -> Memory:
        """任务状态同步为 semantic 记忆（job.<task_id>.progress）。

        幂等 upsert + 版本链自增；TTL 每次顺延（活跃任务不过期）；
        向量尽力刷新（embedding 不可用保留旧向量，与面板编辑口径一致）。

        Args:
            db: 数据库会话。
            task: 最新任务状态。
            user_id: 操作者（memories.user_id 非空，进度记忆归属该用户）。
            old_status: 变更前状态（首建传 None，描述文案区分"新建"）。

        Returns:
            落库后的记忆条目。
        """
        # String 列回读为 str：统一经 TaskStatus(...) 归一后取值（StrEnum 安全）
        new_status = TaskStatus(task.status)
        verb = (
            "新建" if old_status is None else f"{TaskStatus(old_status).value} → {new_status.value}"
        )
        due = f"，截止 {task.due_date:%Y-%m-%d}" if task.due_date else ""
        content = f"任务「{task.title}」{verb}{due}（优先级 {task.priority}）"
        memory = await memory_repo.upsert_task_progress(
            db,
            ws_id=task.workspace_id,
            user_id=user_id,
            task_id=task.id,
            content=content,
            ttl_days=get_settings().task_progress_ttl_days,
        )
        await self._refresh_embedding(db, memory)
        return memory

    async def active_brief(self, db: AsyncSession, *, ws_id: uuid.UUID) -> list[Task]:
        """开场简报：未完成任务（高优先在前，条数受 task_brief_limit 约束）。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。

        Returns:
            未完成任务列表。
        """
        return await task_repo.unfinished_brief(
            db, ws_id=ws_id, limit=get_settings().task_brief_limit
        )

    async def _refresh_embedding(self, db: AsyncSession, memory: Memory) -> None:
        """尽力刷新记忆向量（不可用降级保留旧向量，不阻断任务链路）。

        Args:
            db: 数据库会话。
            memory: 待刷新的记忆条目。
        """
        try:
            client = get_embedding_client()
        except EmbeddingError:
            return  # 未配置属正常部署形态
        try:
            memory.embedding = (await client.embed([memory.content]))[0]
            await db.flush()
        except EmbeddingError as exc:
            logger.warning("task_progress_embedding_degraded key=%s error=%s", memory.key, exc)


task_service = TaskService()
