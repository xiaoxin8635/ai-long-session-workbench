"""message 仓储（append-only：无更新/删除端点，只有插入与查询）。"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Message


async def create(
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    role: str,
    content: str,
    token_count: int,
    metadata_: dict,
) -> Message:
    """插入消息并返回（token_count 由服务层计算）。"""
    message = Message(
        session_id=session_id,
        role=role,
        content=content,
        token_count=token_count,
        meta=metadata_,
    )
    db.add(message)
    await db.flush()
    await db.refresh(message)
    return message


async def list_by_session(
    db: AsyncSession, *, session_id: uuid.UUID, limit: int, offset: int
) -> tuple[list[Message], int]:
    """按时间正序分页取消息。

    Returns:
        (消息列表, 总数)。
    """
    total = await db.scalar(
        select(func.count()).select_from(Message).where(Message.session_id == session_id)
    )
    rows = await db.execute(
        select(Message)
        .where(Message.session_id == session_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all()), int(total or 0)
