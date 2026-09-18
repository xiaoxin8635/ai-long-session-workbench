"""memory 仓储（M-03 级联归档；M-06 滚动摘要 episodic upsert；M-04 抽取/检索管线）。

抽取管线（去重/冲突/入库）与检索管线的全部数据访问收口在此。
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MemoryEventSource, MemoryEventType, MemoryStatus, MemoryType
from app.models.memory import Memory, MemoryEvent

# 会话滚动摘要对应的记忆 key（唯一性依据：workspace + key）
_SUMMARY_KEY_PREFIX = "session_summary:"

# 检索召回的记忆类型（working 在 Redis、knowledge 在向量库，均不走本表）
_RETRIEVAL_TYPES = (MemoryType.SEMANTIC, MemoryType.EPISODIC, MemoryType.PROCEDURAL)


def summary_memory_key(session_id: uuid.UUID) -> str:
    """构造会话摘要记忆的 key。

    Args:
        session_id: 目标会话。

    Returns:
        形如 session_summary:<uuid> 的结构化 key。
    """
    return f"{_SUMMARY_KEY_PREFIX}{session_id}"


async def _add_event(
    db: AsyncSession,
    memory_id: uuid.UUID,
    event_type: MemoryEventType,
    *,
    new_value: str | None = None,
    old_value: str | None = None,
    source: MemoryEventSource = MemoryEventSource.LLM,
) -> None:
    """追加一条记忆事件流水（flush 保证外键顺序，commit 由调用方统一）。"""
    db.add(
        MemoryEvent(
            memory_id=memory_id,
            event_type=event_type,
            new_value=new_value,
            old_value=old_value,
            source=source,
        )
    )
    await db.flush()


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
        await _add_event(
            db,
            memory.id,
            MemoryEventType.CREATED,
            new_value=content,
            source=MemoryEventSource.SYSTEM,
        )
        return memory
    existing.content = content
    existing.status = MemoryStatus.ACTIVE
    existing.version += 1
    await db.flush()
    await _add_event(
        db,
        existing.id,
        MemoryEventType.UPDATED,
        new_value=content,
        source=MemoryEventSource.SYSTEM,
    )
    return existing


# ---- M-04 抽取/检索管线 ----


async def find_active_by_key(db: AsyncSession, *, ws_id: uuid.UUID, key: str) -> Memory | None:
    """按 workspace + key 查找 active 记忆（去重的精确匹配路径）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace（隔离边界）。
        key: 结构化主题键。

    Returns:
        命中的记忆条目；无则 None。
    """
    result = await db.execute(
        select(Memory).where(
            Memory.workspace_id == ws_id,
            Memory.key == key,
            Memory.status == MemoryStatus.ACTIVE,
        )
    )
    return result.scalar_one_or_none()


async def find_similar(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    embedding: list[float],
    threshold: float,
    limit: int = 3,
) -> list[tuple[Memory, float]]:
    """向量近邻查找 active 记忆（去重的相似度路径，复用 HNSW 索引）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        embedding: 候选事实的向量。
        threshold: 余弦相似度阈值（仅返回高于该值的结果）。
        limit: 返回条数上限。

    Returns:
        (记忆, 相似度) 列表，相似度降序。
    """
    similarity = 1 - Memory.embedding.cosine_distance(embedding)
    result = await db.execute(
        select(Memory, similarity.label("sim"))
        .where(
            Memory.workspace_id == ws_id,
            Memory.status == MemoryStatus.ACTIVE,
            Memory.embedding.is_not(None),
            similarity >= threshold,
        )
        .order_by(Memory.embedding.cosine_distance(embedding))
        .limit(limit)
    )
    return [(row[0], float(row[1])) for row in result.all()]


async def create_memory(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    memory_type: MemoryType,
    key: str,
    content: str,
    confidence: float,
    importance: float,
    source_session_id: uuid.UUID | None = None,
    source_message_ids: list[uuid.UUID] | None = None,
    embedding: list[float] | None = None,
    expires_at: datetime | None = None,
    supersedes_id: uuid.UUID | None = None,
) -> Memory:
    """写入一条新记忆并记 CREATED 事件（抽取管线第 ⑤ 步）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        user_id: 归属用户。
        memory_type: 记忆类型。
        key: 结构化主题键。
        content: 事实正文。
        confidence: 置信度。
        importance: 重要性。
        source_session_id: 来源会话。
        source_message_ids: 支撑消息 ID 列表。
        embedding: 内容向量（服务不可用时 None，降级为无向量）。
        expires_at: TTL 到期时间。
        supersedes_id: 被本条替代的旧记忆 ID（版本链）。

    Returns:
        落库后的 Memory 对象（未提交，由调用方统一 commit）。
    """
    memory = Memory(
        workspace_id=ws_id,
        user_id=user_id,
        memory_type=memory_type,
        key=key,
        content=content,
        confidence=confidence,
        importance=importance,
        status=MemoryStatus.ACTIVE,
        source_session_id=source_session_id,
        source_message_ids=[str(m) for m in source_message_ids or []],
        embedding=embedding,
        expires_at=expires_at,
        supersedes_id=supersedes_id,
    )
    db.add(memory)
    await db.flush()
    await _add_event(db, memory.id, MemoryEventType.CREATED, new_value=content)
    return memory


async def mark_superseded(db: AsyncSession, memory: Memory, *, by_id: uuid.UUID) -> None:
    """旧记忆置 superseded 并挂版本链 + SUPERSEDE 事件。

    Args:
        db: 数据库会话。
        memory: 被替代的旧记忆（ORM 对象，原地更新）。
        by_id: 替代它的新记忆 ID。
    """
    memory.status = MemoryStatus.SUPERSEDED
    await _add_event(
        db,
        memory.id,
        MemoryEventType.SUPERSEDED,
        old_value=memory.content,
        new_value=str(by_id),
    )


async def mark_conflicted(db: AsyncSession, memory: Memory) -> None:
    """记忆置 conflicted（待用户裁决）+ CONFLICT 事件。

    Args:
        db: 数据库会话。
        memory: 冲突的记忆条目（新条目先入库再标记，双条并存可见）。
    """
    memory.status = MemoryStatus.CONFLICTED
    await _add_event(db, memory.id, MemoryEventType.CONFLICT, old_value=memory.content)


async def merge_memory(
    db: AsyncSession,
    memory: Memory,
    *,
    content: str,
    confidence: float,
    embedding: list[float] | None,
) -> None:
    """合并结果写回旧条目（content/confidence 更新 + 向量刷新）+ MERGE 事件。

    Args:
        db: 数据库会话。
        memory: 合并目标条目（原地更新，version 自增）。
        content: 合并后的新正文。
        confidence: 提升后的置信度。
        embedding: 新正文的向量（不可用时 None 保留旧向量）。
    """
    old_content = memory.content
    memory.content = content
    memory.confidence = confidence
    memory.version += 1
    if embedding is not None:
        memory.embedding = embedding
    await db.flush()
    await _add_event(
        db,
        memory.id,
        MemoryEventType.MERGED,
        old_value=old_content,
        new_value=content,
    )


async def search_candidates(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    embedding: list[float],
    candidate_k: int,
) -> list[tuple[Memory, float]]:
    """检索读路径的向量召回：active/未过期/归属过滤 + 余弦近邻 top-k。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace（隔离第一道防线）。
        user_id: 归属用户（记忆按用户隔离）。
        embedding: query 向量。
        candidate_k: 召回条数（重排在 Python 侧进行）。

    Returns:
        (记忆, 相似度) 列表，相似度降序。
    """
    now = datetime.now(UTC)
    similarity = 1 - Memory.embedding.cosine_distance(embedding)
    result = await db.execute(
        select(Memory, similarity.label("sim"))
        .where(
            Memory.workspace_id == ws_id,
            Memory.user_id == user_id,
            Memory.status == MemoryStatus.ACTIVE,
            Memory.memory_type.in_(_RETRIEVAL_TYPES),
            Memory.embedding.is_not(None),
            or_(Memory.expires_at.is_(None), Memory.expires_at > now),
        )
        .order_by(Memory.embedding.cosine_distance(embedding))
        .limit(candidate_k)
    )
    return [(row[0], float(row[1])) for row in result.all()]


async def record_hits(db: AsyncSession, memory_ids: list[uuid.UUID]) -> int:
    """批量更新命中统计（hit_count+1、last_hit_at=now）并记 HIT 事件。

    Args:
        db: 数据库会话。
        memory_ids: 本轮被注入上下文的记忆 ID 列表。

    Returns:
        更新条数。
    """
    if not memory_ids:
        return 0
    now = datetime.now(UTC)
    await db.execute(
        update(Memory)
        .where(Memory.id.in_(memory_ids))
        .values(hit_count=Memory.hit_count + 1, last_hit_at=now)
    )
    for mid in memory_ids:
        await _add_event(db, mid, MemoryEventType.HIT, source=MemoryEventSource.SYSTEM)
    return len(memory_ids)


async def count_active(db: AsyncSession, *, ws_id: uuid.UUID) -> int:
    """统计 workspace 下 active 记忆条数（测试与观测用）。"""
    result = await db.execute(
        select(func.count())
        .select_from(Memory)
        .where(Memory.workspace_id == ws_id, Memory.status == MemoryStatus.ACTIVE)
    )
    return int(result.scalar_one())
