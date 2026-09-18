"""session 仓储（隔离第一道防线：所有查询强制 workspace_id 过滤）。"""

import uuid

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import SessionStatus
from app.models.session import Session


async def create(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID, title: str
) -> Session:
    """创建会话（title 由服务层决定默认值）。"""
    session = Session(workspace_id=workspace_id, user_id=user_id, title=title)
    db.add(session)
    await db.flush()
    await db.refresh(session)
    return session


async def get_by_id(
    db: AsyncSession, *, workspace_id: uuid.UUID, session_id: uuid.UUID
) -> Session | None:
    """按主键取会话（强制 workspace 归属，跨 workspace 访问视同不存在）。"""
    result = await db.execute(
        select(Session).where(Session.id == session_id, Session.workspace_id == workspace_id)
    )
    return result.scalar_one_or_none()


async def list_paginated(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    limit: int,
    offset: int,
    keyword: str | None = None,
    include_archived: bool = False,
) -> tuple[list[Session], int]:
    """分页列出会话（默认排除 deleted；include_archived=False 时也排除 archived）。

    Returns:
        (会话列表, 总数) —— 总数按同条件 count，供分页容器使用。
    """
    conditions = [Session.workspace_id == workspace_id, Session.status != SessionStatus.DELETED]
    if not include_archived:
        conditions.append(Session.status != SessionStatus.ARCHIVED)
    if keyword:
        conditions.append(Session.title.ilike(f"%{keyword}%"))

    total = await db.scalar(select(func.count()).select_from(Session).where(*conditions))
    rows = await db.execute(
        select(Session)
        .where(*conditions)
        .order_by(Session.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all()), int(total or 0)


async def soft_delete(db: AsyncSession, session: Session) -> None:
    """软删除会话（status → deleted，数据保留供审计与来源追溯）。"""
    session.status = SessionStatus.DELETED
    await db.flush()


async def bump_counters(
    db: AsyncSession, *, session_id: uuid.UUID, expected_version: int, add_tokens: int
) -> bool:
    """追加消息后原子更新会话计数（乐观锁条件更新）。

    Returns:
        True —— 更新成功（version 前移）；
        False —— 版本不匹配（并发写冲突，由服务层转 409）。
    """
    result = await db.execute(
        update(Session)
        .where(Session.id == session_id, Session.version == expected_version)
        .values(
            token_total=Session.token_total + add_tokens,
            last_message_at=func.now(),
            version=expected_version + 1,
            updated_at=func.now(),
        )
    )
    return result.rowcount == 1
