"""M-10 任务与待办测试（docs/06 §12）。

覆盖：REST CRUD（含 related_session_ids 挂链、due_date null 清除）、
状态变更同步 job.*.progress 记忆（Task Continuity）、开场简报注入
semantic 桶（新会话"继续上次的事"的数据来源）。
"""

import uuid

from httpx import AsyncClient

from app.context.builder import ContextBuilder
from app.context.schemas import SectionKey
from app.db.session import get_session_factory
from app.models.enums import MemberRole, MemoryType, TaskStatus
from app.repositories import memory_repo, task_repo, user_repo, workspace_repo

_PASSWORD = "passw0rd123"


async def _auth_setup(client: AsyncClient, username: str) -> tuple[dict, str]:
    """注册登录并取个人 workspace，返回 (headers, ws_id)。"""
    await client.post("/api/auth/register", json={"username": username, "password": _PASSWORD})
    resp = await client.post("/api/auth/login", json={"username": username, "password": _PASSWORD})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    ws = (await client.get("/api/workspaces", headers=headers)).json()[0]
    return headers, ws["id"]


async def test_task_crud_flow(client: AsyncClient) -> None:
    """创建 → 列表 → 更新（PATCH 语义 + 会话挂链 + null 清除截止）。"""
    headers, ws_id = await _auth_setup(client, "task_crud")
    created = await client.post(
        "/api/tasks",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"title": "准备字节面试", "priority": 1},
    )
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] == TaskStatus.OPEN.value
    assert task["priority"] == 1
    task_id = task["id"]

    listing = await client.get("/api/tasks", headers=headers, params={"workspace_id": ws_id})
    assert [t["id"] for t in listing.json()] == [task_id]

    sid = str(uuid.uuid4())
    patched = await client.patch(
        f"/api/tasks/{task_id}",
        headers=headers,
        params={"workspace_id": ws_id, "session_id": sid},
        json={"status": TaskStatus.DOING.value, "due_date": None},
    )
    assert patched.status_code == 200
    assert patched.json()["status"] == TaskStatus.DOING.value
    assert patched.json()["due_date"] is None
    assert sid in patched.json()["related_session_ids"]

    done = await client.patch(
        f"/api/tasks/{task_id}",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"status": TaskStatus.DONE.value},
    )
    assert done.json()["status"] == TaskStatus.DONE.value

    missing = await client.patch(
        f"/api/tasks/{uuid.uuid4()}",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"status": TaskStatus.DOING.value},
    )
    assert missing.status_code == 404


async def test_task_status_change_syncs_memory(client: AsyncClient) -> None:
    """状态变更同步 job.<task_id>.progress 语义记忆（版本链自增）。"""
    headers, ws_id = await _auth_setup(client, "task_memory")
    created = await client.post(
        "/api/tasks",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"title": "写周报"},
    )
    task_id = uuid.UUID(created.json()["id"])
    await client.patch(
        f"/api/tasks/{task_id}",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"status": TaskStatus.DOING.value},
    )

    async with get_session_factory()() as db:
        memory = await memory_repo.find_active_by_key(
            db, ws_id=uuid.UUID(ws_id), key=memory_repo.task_progress_key(task_id)
        )
        assert memory is not None
        assert "open → doing" in memory.content
        assert memory.memory_type == MemoryType.SEMANTIC  # String 列回读 str，== 比较
        assert memory.expires_at is not None  # TTL 顺延
        version_before = memory.version
    await client.patch(
        f"/api/tasks/{task_id}",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"status": TaskStatus.DONE.value},
    )
    async with get_session_factory()() as db:
        refreshed = await memory_repo.find_active_by_key(
            db, ws_id=uuid.UUID(ws_id), key=memory_repo.task_progress_key(task_id)
        )
        assert refreshed is not None and refreshed.version == version_before + 1


async def test_active_brief_injected_into_context(client: AsyncClient) -> None:
    """开场简报：未完成任务进 semantic 桶（高优先在前），已完成不注入。"""
    headers, ws_id = await _auth_setup(client, "task_brief")
    for title, priority in [("普通任务", 0), ("高优先任务", 1)]:
        await client.post(
            "/api/tasks",
            headers=headers,
            params={"workspace_id": ws_id},
            json={"title": title, "priority": priority},
        )
    listing = (
        await client.get("/api/tasks", headers=headers, params={"workspace_id": ws_id})
    ).json()
    high = next(t for t in listing if t["priority"] == 1)
    await client.patch(
        f"/api/tasks/{high['id']}",
        headers=headers,
        params={"workspace_id": ws_id},
        json={"status": TaskStatus.DONE.value},
    )

    class _FakeWM:
        """窗口恒返回当前问题的假 Working Memory。"""

        async def window(self, session_id: uuid.UUID, budget_tokens: int) -> list[dict]:
            """单条用户消息窗口。"""
            return [{"role": "user", "content": "继续上次的事"}]

    async with get_session_factory()() as db:
        builder = ContextBuilder(_FakeWM(), retriever=None, knowledge=None)
        assembled = await builder.build(
            db,
            ws_id=uuid.UUID(ws_id),
            user_id=uuid.uuid4(),
            session_id=uuid.uuid4(),
            query="继续上次的事",
        )
    system = assembled.messages[0]["content"]
    assert "未完成任务「普通任务」" in system  # semantic 桶渲染
    assert "高优先任务" not in system  # 已完成任务不注入
    sem = next(s for s in assembled.sections if s.key == SectionKey.SEMANTIC)
    assert sem.included >= 1  # 任务简报条目计入 semantic 区块


async def test_unfinished_brief_ordering() -> None:
    """仓储层排序：优先级降序在前；已完成任务排除。"""
    async with get_session_factory()() as db:
        user = await user_repo.create(
            db, username=f"brief{uuid.uuid4().hex[:8]}", password_hash="x" * 60
        )
        ws = await workspace_repo.create(db, name="简报排序空间", owner_id=user.id)
        await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
        ws_id = ws.id
        normal = await task_repo.create_task(
            db, ws_id=ws_id, title="普通", priority=0, due_date=None, related_session_ids=[]
        )
        high = await task_repo.create_task(
            db, ws_id=ws_id, title="高优", priority=1, due_date=None, related_session_ids=[]
        )
        finished = await task_repo.create_task(
            db, ws_id=ws_id, title="已完成", priority=1, due_date=None, related_session_ids=[]
        )
        finished.status = TaskStatus.DONE
        await db.commit()
        brief = await task_repo.unfinished_brief(db, ws_id=ws_id, limit=5)
        assert [t.id for t in brief] == [high.id, normal.id]  # 高优先在前，done 排除
        limited = await task_repo.unfinished_brief(db, ws_id=ws_id, limit=1)
        assert [t.id for t in limited] == [high.id]
