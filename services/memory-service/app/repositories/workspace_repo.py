"""workspace 仓储：隔离第一道防线的强制执行点。

所有查询均以 workspace_id / user_id 过滤；服务层在此基础上
做成员资格与角色校验（第二道防线，见 app/core/deps.py）。
"""
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MemberRole
from app.models.user import User, Workspace, WorkspaceMember


async def create(db: AsyncSession, *, name: str, owner_id: uuid.UUID) -> Workspace:
    """创建 workspace。"""
    ws = Workspace(name=name, owner_id=owner_id)
    db.add(ws)
    await db.flush()
    await db.refresh(ws)
    return ws


async def get_by_id(db: AsyncSession, ws_id: uuid.UUID) -> Workspace | None:
    """按主键查询 workspace。"""
    return await db.get(Workspace, ws_id)


async def add_member(
    db: AsyncSession, *, ws_id: uuid.UUID, user_id: uuid.UUID, role: MemberRole
) -> WorkspaceMember:
    """添加成员（存在则更新角色）。"""
    member = await get_member(db, ws_id=ws_id, user_id=user_id)
    if member is not None:
        member.role = role
        await db.flush()
        return member
    member = WorkspaceMember(workspace_id=ws_id, user_id=user_id, role=role)
    db.add(member)
    await db.flush()
    await db.refresh(member)
    return member


async def get_member(
    db: AsyncSession, *, ws_id: uuid.UUID, user_id: uuid.UUID
) -> WorkspaceMember | None:
    """查询成员关系（成员校验与 RBAC 的数据来源）。"""
    result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    return result.scalar_one_or_none()


async def list_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[Workspace]:
    """列出用户所属的全部 workspace（会话列表页入口）。"""
    result = await db.execute(
        select(Workspace)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(Workspace.created_at)
    )
    return list(result.scalars().all())


async def list_members(db: AsyncSession, ws_id: uuid.UUID) -> list[tuple[WorkspaceMember, User]]:
    """列出 workspace 全部成员（联出用户名，管理面板用）。"""
    result = await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == ws_id)
        .order_by(WorkspaceMember.created_at)
    )
    return list(result.all())
