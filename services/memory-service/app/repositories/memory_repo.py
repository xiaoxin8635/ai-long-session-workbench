"""memory 仓储（M-03 级联归档；M-06 滚动摘要的 episodic 条目 upsert）。

完整记忆管线（抽取/检索/冲突）见 M-04。
"""

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MemoryEventSource, MemoryEventType, MemoryStatus, MemoryType
from app.models.memory import Memory, MemoryEvent

# 会话滚动摘要对应的记忆 key（唯一性依据：workspace + key）
_SUMMARY_KEY_PREFIX = "session_summary:"


def summary_memory_key(session_id: uuid.UUID) -> str:
    """构造会话摘要记忆的 key。

    Args:
        session_id: 目标会话。

    Returns:
        形如 session_summary:<uuid> 的结构化 key。
    """
    return f"{_SUMMARY_KEY_PREFIX}{session_id}"


async def archive_by_source_session(db: AsyncSession, *, session_id: uuid.UUID) -> int:
    """归档某会话产出的全部记忆（删除会话 cascade_memories=True 时调用）。

    Returns:
        归档行数（审计与测试断言用）。
    """
    result = await db.execute(
        update(Memory)
        .where(Memory.source_session_id == session_id)
        .values(status=MemoryStatus.ARCHIVED)
    )
    return result.rowcount or 0


async def upsert_episodic_summary(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    content: str,
) -> Memory:
    """写入/更新会话滚动摘要的 episodic 记忆条目（版本链自增，幂等）。

    按 (workspace_id, key=session_summary:<sid>) 查找：存在则更新内容并
    version+1、状态回 ACTIVE；不存在则创建。同时写 memory_events 流水。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        session_id: 摘要来源会话。
        user_id: 会话创建者（memories.user_id 非空，摘要记忆归属该用户）。
        content: 最新摘要全文。

    Returns:
        落库后的 Memory 对象。
    """
    key = summary_memory_key(session_id)
    existing = (
        await db.execute(select(Memory).where(Memory.workspace_id == ws_id, Memory.key == key))
    ).scalar_one_or_none()
    if existing is None:
        memory = Memory(
            workspace_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.EPISODIC,
            key=key,
            content=content,
            source_session_id=session_id,
            confidence=0.9,
            importance=0.7,
            status=MemoryStatus.ACTIVE,
        )
        db.add(memory)
        await db.flush()
        db.add(
            MemoryEvent(
                memory_id=memory.id,
                event_type=MemoryEventType.CREATED,
                new_value=content,
                source=MemoryEventSource.SYSTEM,
            )
        )
        return memory
    existing.content = content
    existing.status = MemoryStatus.ACTIVE
    existing.version += 1
    await db.flush()
    db.add(
        MemoryEvent(
            memory_id=existing.id,
            event_type=MemoryEventType.UPDATED,
            new_value=content,
            source=MemoryEventSource.SYSTEM,
        )
    )
    return existing
