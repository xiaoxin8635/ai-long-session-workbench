"""记忆抽取管线编排（M-04 写路径，docs/01 §5.2.2）。

extract→打分过滤→批量向量化→逐条去重→冲突仲裁→入库，全流程异常降级
（抽取失败/向量不可用不阻塞主对话；由 ChatService 在后台任务中调用）。

仲裁动作落库语义：
  - MERGE：合并进旧条目（content/confidence 更新、version 自增）
    （仅限 strict 判重命中——key 精确或相似度 ≥ 判重阈值）
  - SUPERSEDE：新条目入库挂 supersedes_id，旧条目置 superseded（版本链）
  - COEXIST：新条目入库后双条置 conflicted，等待用户在记忆面板裁决
    （含疑似冲突层命中时 MERGE 的降级出口，Fix A）

事务与并发（Fix D，M-13 实测死锁修复）：
  - 候选逐条独立提交，锁持有窗口缩至单条毫秒级，避免与并发
    record_hits 等 UPDATE 交叉持锁
  - PG 死锁（SQLSTATE 40P01）回滚后重试一次（被牺牲事务已由 PG 回滚）
  - 生产入口按 (ws_id, user_id) 进程内加锁串行化，同用户并发轮次
    不再同时持多行锁互相等待
"""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.llm.client import get_extractor_client
from app.llm.embeddings import EmbeddingClient, EmbeddingError, get_embedding_client
from app.memory.conflict import ConflictResolver
from app.memory.deduplicator import MemoryDeduplicator
from app.memory.extractor import MemoryExtractor
from app.memory.schemas import ConflictAction, ConflictOutcome, MemoryCandidate
from app.models.memory import Memory
from app.observability import tracing
from app.repositories import memory_repo

logger = logging.getLogger(__name__)

# 每 (ws_id, user_id) 一把抽取锁（Fix D）：同用户并发轮次串行进入管线，
# 从源头避免两条管线同时持多行锁互相等待。锁对象随活跃用户数增长，
# 量级为进程生命周期内的活跃用户数，内存开销可忽略，不做回收。
_EXTRACT_LOCKS: dict[tuple[uuid.UUID, uuid.UUID], asyncio.Lock] = {}

# PG 死锁的 SQLSTATE（deadlock_detected）；驱动（asyncpg）原始异常携带
_DEADLOCK_SQLSTATE = "40P01"


def _is_deadlock(exc: BaseException) -> bool:
    """判定 SQLAlchemy 包装的 DBAPI 异常是否为 PG 死锁。

    Args:
        exc: 捕获的异常（DBAPIError 时 orig 属性为驱动原始异常）。

    Returns:
        True —— 死锁（被牺牲事务已由 PG 回滚，可安全重试）；其余 False。
    """
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    return str(getattr(orig, "sqlstate", "")) == _DEADLOCK_SQLSTATE


class ExtractPipeline:
    """一轮对话 → 长期记忆 的完整写路径编排器。"""

    def __init__(
        self,
        extractor: MemoryExtractor,
        deduplicator: MemoryDeduplicator,
        resolver: ConflictResolver,
        embedding: EmbeddingClient,
    ) -> None:
        """注入管线各组件（生产默认组装见 extract_pipeline 工厂，测试可替换 fake）。

        Args:
            extractor: LLM 事实抽取器。
            deduplicator: key/向量去重器。
            resolver: 冲突仲裁器。
            embedding: 向量化客户端。
        """
        self._extractor = extractor
        self._deduplicator = deduplicator
        self._resolver = resolver
        self._embedding = embedding

    async def run(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        messages: Sequence[tuple[uuid.UUID, str, str]],
    ) -> list[Memory]:
        """对一轮对话执行抽取入库（候选逐条独立提交，Fix D）。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            user_id: 归属用户（记忆条目 user_id）。
            session_id: 来源会话。
            messages: (message_id, role, content) 时间正序。

        Returns:
            本轮写入/更新的记忆条目（观测与测试断言用；无抽取结果为空）。
        """
        settings = get_settings()
        try:
            # Fix B：注入既有 key 引导 LLM 复用命名——key 稳定后精确判重
            # 与仲裁链路才能生效（同主题矛盾事实不再因 key 漂移而并存）
            known_keys = await memory_repo.list_active_keys(db, ws_id=ws_id)
            facts = await self._extractor.extract(list(messages), known_keys=known_keys)
        except Exception as exc:  # LLMError / ExtractionError：跳过本轮（降级）
            logger.warning("extract_skipped session=%s error=%s", session_id, exc)
            return []
        facts = [f for f in facts if f.confidence >= settings.extract_min_confidence]
        if not facts:
            return []

        vectors = await self._embed_or_none([f.content for f in facts])
        candidates = [
            MemoryCandidate(
                fact=fact,
                embedding=vectors[i],
                expires_at=(
                    datetime.now(UTC) + timedelta(days=fact.ttl_days)
                    if fact.ttl_days is not None
                    else None
                ),
            )
            for i, fact in enumerate(facts)
        ]

        written: list[Memory] = []
        for candidate in candidates:
            memory = await self._upsert_with_deadlock_retry(
                db,
                ws_id=ws_id,
                user_id=user_id,
                session_id=session_id,
                candidate=candidate,
            )
            if memory is not None:
                written.append(memory)
        logger.info(
            "extract_done ws=%s session=%s facts=%s written=%s",
            ws_id,
            session_id,
            len(facts),
            len(written),
        )
        return written

    async def _upsert_with_deadlock_retry(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        candidate: MemoryCandidate,
        max_retries: int = 2,
    ) -> Memory | None:
        """单候选 upsert + 独立提交；PG 死锁回滚后带退避重试（Fix D）。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            user_id: 归属用户。
            session_id: 来源会话。
            candidate: 入库候选。
            max_retries: 死锁重试上限（实测偶发二次死锁，重试 2 次 + 退避覆盖）。

        Returns:
            写入/更新的记忆条目；无产出 None。

        Raises:
            DBAPIError: 非死锁错误，或重试耗尽仍死锁（外层吞掉整轮）。
        """
        for attempt in range(max_retries + 1):
            try:
                memory = await self._upsert_one(
                    db,
                    ws_id=ws_id,
                    user_id=user_id,
                    session_id=session_id,
                    candidate=candidate,
                )
                # 逐候选独立提交（Fix D）：锁持有窗口缩至单条毫秒级，
                # 且同轮后续候选的判重可见先前写入（同轮重复也能去重）
                await db.commit()
                return memory
            except DBAPIError as exc:
                if not _is_deadlock(exc) or attempt == max_retries:
                    raise
                # PG 已回滚被牺牲的事务：回滚会话、退避让对方事务先完成再重试
                backoff = 0.1 * (attempt + 1)
                logger.warning(
                    "extract_deadlock_retry ws=%s session=%s key=%s attempt=%s backoff=%.1fs",
                    ws_id,
                    session_id,
                    candidate.fact.key,
                    attempt + 1,
                    backoff,
                )
                await db.rollback()
                await asyncio.sleep(backoff)
        # 循环不可自然退出：末轮要么 return（成功）要么 raise（重试耗尽/非死锁）
        raise RuntimeError("unreachable: deadlock retry loop exited")

    async def _embed_or_none(self, texts: list[str]) -> list[list[float] | None]:
        """批量向量化；失败时整体降级为 None 列表（仅 key 判重、无向量入库）。"""
        try:
            vectors = await self._embedding.embed(texts)
        except EmbeddingError as exc:
            logger.warning("extract_embedding_degraded error=%s", exc)
            return [None] * len(texts)
        return list(vectors)

    async def _upsert_one(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        session_id: uuid.UUID,
        candidate: MemoryCandidate,
    ) -> Memory | None:
        """单条候选的 去重→仲裁→入库（事务由 run 逐候选提交）。"""
        fact = candidate.fact
        duplicate = await self._deduplicator.find_duplicate(
            db, ws_id=ws_id, key=fact.key, embedding=candidate.embedding
        )
        if duplicate is None:
            return await memory_repo.create_memory(
                db,
                ws_id=ws_id,
                user_id=user_id,
                memory_type=fact.memory_type,
                key=fact.key,
                content=fact.content,
                confidence=fact.confidence,
                importance=fact.importance,
                source_session_id=session_id,
                source_message_ids=fact.source_message_ids,
                embedding=candidate.embedding,
                expires_at=candidate.expires_at,
            )

        old = duplicate.memory
        outcome = await self._resolver.resolve(old, fact)
        if outcome.action == ConflictAction.MERGE and not duplicate.strict:
            # 疑似冲突层禁止 MERGE（Fix A）：相似但非同 key 的条目可能是
            # 同主题的不同侧面，合并会丢失信息 —— 降级 COEXIST 交用户裁决
            logger.info(
                "extract_suspect_merge_downgraded key=%s old_id=%s sim=%.3f",
                fact.key,
                old.id,
                duplicate.similarity,
            )
            outcome = ConflictOutcome(action=ConflictAction.COEXIST)
        if outcome.action == ConflictAction.MERGE:
            await memory_repo.merge_memory(
                db,
                old,
                content=outcome.merged_content or fact.content,
                confidence=outcome.merged_confidence or fact.confidence,
                embedding=candidate.embedding,
            )
            return old
        new_memory = await memory_repo.create_memory(
            db,
            ws_id=ws_id,
            user_id=user_id,
            memory_type=fact.memory_type,
            key=fact.key,
            content=fact.content,
            confidence=fact.confidence,
            importance=fact.importance,
            source_session_id=session_id,
            source_message_ids=fact.source_message_ids,
            embedding=candidate.embedding,
            expires_at=candidate.expires_at,
            # SUPERSEDE 时挂版本链（新条 → 被替代的旧条），COEXIST 不挂
            supersedes_id=old.id if outcome.action == ConflictAction.SUPERSEDE else None,
        )
        if outcome.action == ConflictAction.SUPERSEDE:
            await memory_repo.mark_superseded(db, old, by_id=new_memory.id)
        else:  # COEXIST：双条 conflicted 待用户裁决
            await memory_repo.mark_conflicted(db, old)
            await memory_repo.mark_conflicted(db, new_memory)
        return new_memory


# ---- 生产默认组装与后台任务入口 ----


async def extract_pipeline(
    *,
    ws_id: uuid.UUID,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    messages: Sequence[tuple[uuid.UUID, str, str]],
) -> list[Memory]:
    """生产组装的抽取管线入口（自管理数据库会话，供后台任务调用）。

    Args:
        ws_id: 所属 workspace。
        user_id: 归属用户。
        session_id: 来源会话。
        messages: (message_id, role, content) 时间正序。

    Returns:
        写入/更新的记忆条目；任何异常在内部吞掉（后台任务不外抛）。
    """
    try:
        embedding_client = get_embedding_client()
    except EmbeddingError:
        logger.warning("extract_pipeline_skip_embedding_not_configured")
        return []
    pipeline = ExtractPipeline(
        extractor=MemoryExtractor(get_extractor_client()),
        deduplicator=MemoryDeduplicator(),
        resolver=ConflictResolver(get_extractor_client()),
        embedding=embedding_client,
    )
    # Fix D：同用户并发轮次串行进入管线（见 _EXTRACT_LOCKS 说明）
    lock = _EXTRACT_LOCKS.setdefault((ws_id, user_id), asyncio.Lock())
    async with lock:
        try:
            async with get_session_factory()() as db:
                # memory.extract span（M-12）：后台任务继承触发点的上下文快照，
                # 正常挂触发的 chat.turn 之下；facts/written 计数完成后上报
                with tracing.span(
                    "memory.extract",
                    workspace_id=str(ws_id),
                    session_id=str(session_id),
                    model=get_settings().extractor_model or get_settings().llm_model,
                ) as obs:
                    written = await pipeline.run(
                        db, ws_id=ws_id, user_id=user_id, session_id=session_id, messages=messages
                    )
                    obs.update(metadata={"written": len(written)})
                    return written
        except Exception:
            logger.exception("extract_pipeline_failed ws=%s session=%s", ws_id, session_id)
            return []
