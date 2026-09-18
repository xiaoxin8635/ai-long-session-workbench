"""memory 仓储（M-03 阶段仅提供删除会话级联归档；完整管线见 M-04）。"""
import uuid

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MemoryStatus
from app.models.memory import Memory


async def archive_by_source_session(
    db: AsyncSession, *, session_id: uuid.UUID
) -> int:
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
