"""memory 仓储（M-03 级联归档；M-06 滚动摘要 episodic upsert；M-04 抽取/检索管线；
M-11 记忆面板管理：列表过滤/版本链/用户编辑/软删除/冲突裁决；
M-10 任务进度 semantic upsert——job.*.progress，Task Continuity 记忆侧）。

抽取管线（去重/冲突/入库）、检索管线与管理面板的全部数据访问收口在此。
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, or_, select, update
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


def task_progress_key(task_id: uuid.UUID) -> str:
    """任务进度记忆的结构化主题键（docs/01 §5.7：job.*.progress）。"""
    return f"job:{task_id}.progress"


async def upsert_task_progress(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    content: str,
    ttl_days: int,
) -> Memory:
    """写入/更新任务进度的 semantic 记忆条目（M-10，幂等版本链增）。

    按 (workspace_id, key=job:<task_id>.progress) 查找：存在则更新内容并
    version+1、状态回 ACTIVE、TTL 顺延（活跃任务不过期）；不存在则创建。
    同时写 memory_events 流水（source=SYSTEM，区别于 LLM 抽取）。
    向量补齐由调用方（TaskService）尽力刷新，本函数只管数据形态。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        user_id: 任务操作者（memories.user_id 非空，进度记忆归属该用户）。
        task_id: 任务 ID（key 的一部分）。
        content: 进度描述全文。
        ttl_days: TTL 天数（进度类事实默认 30 天）。

    Returns:
        落库后的 Memory 对象。
    """
    key = task_progress_key(task_id)
    expires_at = datetime.now(UTC) + timedelta(days=ttl_days)
    existing = (
        await db.execute(select(Memory).where(Memory.workspace_id == ws_id, Memory.key == key))
    ).scalar_one_or_none()
    if existing is None:
        memory = Memory(
            workspace_id=ws_id,
            user_id=user_id,
            memory_type=MemoryType.SEMANTIC,
            key=key,
            content=content,
            confidence=0.9,
            importance=0.8,
            expires_at=expires_at,
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
    existing.expires_at = expires_at
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


# ---- M-11 记忆面板管理 ----


def _list_filters(
    ws_id: uuid.UUID,
    memory_type: MemoryType | None,
    status: MemoryStatus | None,
    keyword: str | None,
) -> list[object]:
    """构造列表查询的公共过滤条件（软删除始终排除，与面板口径一致）。"""
    filters: list[object] = [Memory.workspace_id == ws_id, Memory.status != MemoryStatus.DELETED]
    if memory_type is not None:
        filters.append(Memory.memory_type == memory_type)
    if status is not None:
        filters.append(Memory.status == status)
    if keyword:
        # 关键词对 key 与 content 做不区分大小写的包含匹配
        pattern = f"%{keyword}%"
        filters.append(or_(Memory.key.ilike(pattern), Memory.content.ilike(pattern)))
    return filters


async def list_memories(
    db: AsyncSession,
    *,
    ws_id: uuid.UUID,
    memory_type: MemoryType | None = None,
    status: MemoryStatus | None = None,
    keyword: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[Memory], int]:
    """记忆列表（面板主查询：过滤 + 分页；conflicted 置顶）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        memory_type: 类型过滤；None 不过滤。
        status: 状态过滤；None 不过滤（软删除始终排除）。
        keyword: key/content 关键词（ILIKE）；None 不过滤。
        limit: 页大小。
        offset: 偏移量。

    Returns:
        (条目列表, 总数)；列表按 conflicted 置顶 + updated_at 倒序。
    """
    filters = _list_filters(ws_id, memory_type, status, keyword)
    total = int(
        (await db.execute(select(func.count()).select_from(Memory).where(*filters))).scalar_one()
    )
    result = await db.execute(
        select(Memory)
        .where(*filters)
        .order_by(
            case((Memory.status == MemoryStatus.CONFLICTED, 0), else_=1),
            Memory.updated_at.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total


async def get_by_id(db: AsyncSession, *, ws_id: uuid.UUID, memory_id: uuid.UUID) -> Memory | None:
    """按 ID 取单条记忆（workspace 隔离；软删除条目仍可查，供详情溯源）。

    Args:
        db: 数据库会话。
        ws_id: 所属 workspace。
        memory_id: 记忆 ID。

    Returns:
        命中的记忆条目；无则 None。
    """
    result = await db.execute(
        select(Memory).where(Memory.workspace_id == ws_id, Memory.id == memory_id)
    )
    return result.scalar_one_or_none()


async def version_chain(db: AsyncSession, memory: Memory) -> list[Memory]:
    """沿 supersedes_id 上溯被替代的历史版本（时间倒序，不含自身）。

    Args:
        db: 数据库会话。
        memory: 起点（当前版本）条目。

    Returns:
        被替代版本列表（最近的在前）；环或断链自动终止。
    """
    chain: list[Memory] = []
    seen = {memory.id}
    current = memory
    while current.supersedes_id is not None and current.supersedes_id not in seen:
        older = (
            await db.execute(select(Memory).where(Memory.id == current.supersedes_id))
        ).scalar_one_or_none()
        if older is None:
            break
        chain.append(older)
        seen.add(older.id)
        current = older
    return chain


async def events_of(
    db: AsyncSession, memory_id: uuid.UUID, *, limit: int = 20
) -> list[MemoryEvent]:
    """某条记忆的事件流水（最新在前，默认 20 条）。

    Args:
        db: 数据库会话。
        memory_id: 记忆 ID。
        limit: 返回条数上限。

    Returns:
        事件列表，created_at 倒序。
    """
    result = await db.execute(
        select(MemoryEvent)
        .where(MemoryEvent.memory_id == memory_id)
        .order_by(MemoryEvent.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def edit_by_user(
    db: AsyncSession,
    memory: Memory,
    *,
    content: str | None = None,
    confidence: float | None = None,
    importance: float | None = None,
    expires_at: datetime | None = None,
    expires_at_set: bool = False,
    embedding: list[float] | None = None,
) -> None:
    """用户手动编辑记忆（按提交字段部分更新，version+1）+ EDITED_BY_USER 审计。

    Args:
        db: 数据库会话。
        memory: 编辑目标（原地更新）。
        content: 新正文；None 不改。
        confidence / importance: 新评分；None 不改。
        expires_at: 新 TTL；expires_at_set=True 且值为 None 表示清除。
        expires_at_set: 请求是否显式提交了 expires_at（区分"未提交"与"提交 null"）。
        embedding: 新正文的向量（None 保留旧向量，与 merge 口径一致）。
    """
    old_content = memory.content
    changed = False
    if content is not None and content != memory.content:
        memory.content = content
        changed = True
    if confidence is not None and confidence != memory.confidence:
        memory.confidence = confidence
        changed = True
    if importance is not None and importance != memory.importance:
        memory.importance = importance
        changed = True
    if expires_at_set and expires_at != memory.expires_at:
        memory.expires_at = expires_at
        changed = True
    if embedding is not None and content is not None:
        memory.embedding = embedding
    if not changed and embedding is None:
        return  # 无任何变更：不落版本与审计
    memory.version += 1
    await db.flush()
    await _add_event(
        db,
        memory.id,
        MemoryEventType.EDITED_BY_USER,
        old_value=old_content,
        new_value=memory.content,
        source=MemoryEventSource.USER,
    )


async def soft_delete(db: AsyncSession, memory: Memory) -> None:
    """软删除记忆（status=deleted，检索与列表均不可见）+ DELETED 审计。

    Args:
        db: 数据库会话。
        memory: 删除目标（原地更新；幂等：已删除直接返回）。
    """
    if memory.status == MemoryStatus.DELETED:
        return
    memory.status = MemoryStatus.DELETED
    await _add_event(
        db,
        memory.id,
        MemoryEventType.DELETED,
        old_value=memory.content,
        source=MemoryEventSource.USER,
    )


async def find_conflict_peer(db: AsyncSession, memory: Memory) -> Memory | None:
    """找同 workspace 同 key 的另一条 conflicted 记忆（裁决对手方）。

    Args:
        db: 数据库会话。
        memory: 裁决起点条目（应为 conflicted）。

    Returns:
        对手方条目；无（不存在或状态不符）则 None。
    """
    result = await db.execute(
        select(Memory).where(
            Memory.workspace_id == memory.workspace_id,
            Memory.key == memory.key,
            Memory.status == MemoryStatus.CONFLICTED,
            Memory.id != memory.id,
        )
    )
    peers = list(result.scalars().all())
    return peers[0] if peers else None


async def resolve_conflict(db: AsyncSession, winner: Memory, loser: Memory) -> None:
    """冲突裁决落库：winner 回 active，loser 置 superseded 并挂版本链。

    版本链方向与抽取管线一致（替代者持有指向被替代者的指针）：
    winner.supersedes_id 在为空时指向 loser；已有链（曾替代更早版本）则不覆盖。
    两条事件来源均记 USER（面板操作，区别于抽取管线自动仲裁）。

    Args:
        db: 数据库会话。
        winner: 用户选择保留的条目（原地更新为 active）。
        loser: 被放弃的条目（原地更新为 superseded）。
    """
    winner.status = MemoryStatus.ACTIVE
    if winner.supersedes_id is None:
        winner.supersedes_id = loser.id
    await _add_event(
        db,
        winner.id,
        MemoryEventType.UPDATED,
        old_value=MemoryStatus.CONFLICTED.value,
        new_value=MemoryStatus.ACTIVE.value,
        source=MemoryEventSource.USER,
    )
    loser.status = MemoryStatus.SUPERSEDED
    await _add_event(
        db,
        loser.id,
        MemoryEventType.SUPERSEDED,
        old_value=loser.content,
        new_value=str(winner.id),
        source=MemoryEventSource.USER,
    )
