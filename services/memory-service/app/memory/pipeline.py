"""记忆抽取管线编排（M-04 写路径，docs/01 §5.2.2）。

extract→打分过滤→批量向量化→逐条去重→冲突仲裁→入库，全流程异常降级
（抽取失败/向量不可用不阻塞主对话；由 ChatService 在后台任务中调用）。

仲裁动作落库语义：
  - MERGE：合并进旧条目（content/confidence 更新、version 自增）
  - SUPERSEDE：新条目入库挂 supersedes_id，旧条目置 superseded（版本链）
  - COEXIST：新条目入库后双条置 conflicted，等待用户在记忆面板裁决
"""

import logging
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.llm.client import get_extractor_client
from app.llm.embeddings import EmbeddingClient, EmbeddingError, get_embedding_client
from app.memory.conflict import ConflictResolver
from app.memory.deduplicator import MemoryDeduplicator
from app.memory.extractor import MemoryExtractor
from app.memory.schemas import ConflictAction, MemoryCandidate
from app.models.memory import Memory
from app.repositories import memory_repo

logger = logging.getLogger(__name__)


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
        """对一轮对话执行抽取入库（db 事务由本方法统一提交）。

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
            facts = await self._extractor.extract(list(messages))
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
            memory = await self._upsert_one(
                db,
                ws_id=ws_id,
                user_id=user_id,
                session_id=session_id,
                candidate=candidate,
            )
            if memory is not None:
                written.append(memory)
        await db.commit()
        logger.info(
            "extract_done ws=%s session=%s facts=%s written=%s",
            ws_id,
            session_id,
            len(facts),
            len(written),
        )
        return written

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
        """单条候选的 去重→仲裁→入库（事务由 run 统一提交）。"""
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
    try:
        async with get_session_factory()() as db:
            return await pipeline.run(
                db, ws_id=ws_id, user_id=user_id, session_id=session_id, messages=messages
            )
    except Exception:
        logger.exception("extract_pipeline_failed ws=%s session=%s", ws_id, session_id)
        return []
