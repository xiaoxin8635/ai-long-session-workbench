"""记忆管理服务（M-11 面板后端编排，docs/01 §7 / docs/05 §7.1）。

在 repo 之上补齐面板语义：
  - 编辑时尽力刷新 embedding（服务不可用降级保留旧向量，与抽取管线 merge 口径一致）
  - 冲突裁决的双向编排（keep=this/other → winner/loser → 版本链）
  - 检索测试复用 M-04 MemoryRetriever（embedding 未配置降级返回空列表）
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AppError, NotFoundError
from app.llm.embeddings import EmbeddingClient, EmbeddingError, get_embedding_client
from app.memory.retriever import MemoryRetriever
from app.memory.schemas import ScoredMemory
from app.models.enums import MemoryStatus, MemoryType
from app.models.memory import Memory, MemoryEvent
from app.repositories import memory_repo
from app.schemas.memory import MemorySearchRequest, MemoryUpdate

logger = logging.getLogger(__name__)


class MemoryAdminService:
    """记忆面板用例编排（查/改/删/裁决/检索测试）。"""

    def __init__(self, embedding: EmbeddingClient | None = None) -> None:
        """注入 Embedding 客户端。

        Args:
            embedding: OpenAI 兼容 /embeddings 客户端；None 时惰性构造，
                未配置（EmbeddingError）则编辑降级保留旧向量、检索测试返回空。
        """
        self._embedding = embedding
        # 检索器三态：_retriever_ready=False 未初始化；初始化后 None=不可用
        self._retriever: MemoryRetriever | None = None
        self._retriever_ready = False

    async def _get_embedding(self) -> EmbeddingClient | None:
        """返回可用的 Embedding 客户端（未配置/不可用返回 None，仅记一次提示）。"""
        if self._embedding is None:
            try:
                self._embedding = get_embedding_client()
            except EmbeddingError:
                logger.info("memory_admin_embedding_not_configured")
                self._embedding = None
        return self._embedding

    async def _get_retriever(self) -> MemoryRetriever | None:
        """返回检索器（embedding 不可用时 None，检索测试降级为空列表）。"""
        if not self._retriever_ready:
            embedding = await self._get_embedding()
            self._retriever = MemoryRetriever(embedding) if embedding else None
            self._retriever_ready = True
        return self._retriever

    async def _require_memory(
        self, db: AsyncSession, *, ws_id: uuid.UUID, memory_id: uuid.UUID
    ) -> Memory:
        """取记忆条目，不存在抛 NotFoundError（404）。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace（隔离边界）。
            memory_id: 记忆 ID。

        Returns:
            记忆 ORM 对象。

        Raises:
            NotFoundError: 条目不存在（或属于其他 workspace）。
        """
        memory = await memory_repo.get_by_id(db, ws_id=ws_id, memory_id=memory_id)
        if memory is None:
            raise NotFoundError("记忆不存在")
        return memory

    async def list_paginated(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        memory_type: MemoryType | None = None,
        status: MemoryStatus | None = None,
        keyword: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[Memory], int]:
        """记忆列表（conflicted 置顶 + updated_at 倒序；软删除排除）。"""
        return await memory_repo.list_memories(
            db,
            ws_id=ws_id,
            memory_type=memory_type,
            status=status,
            keyword=keyword,
            limit=limit,
            offset=offset,
        )

    async def get_detail(
        self, db: AsyncSession, *, ws_id: uuid.UUID, memory_id: uuid.UUID
    ) -> tuple[Memory, list[Memory], list[MemoryEvent]]:
        """记忆详情：基础字段 + 版本链 + 事件流水。

        Returns:
            (记忆, 被替代版本列表[时间倒序], 事件流水[最新在前])。
        """
        memory = await self._require_memory(db, ws_id=ws_id, memory_id=memory_id)
        chain = await memory_repo.version_chain(db, memory)
        events = await memory_repo.events_of(db, memory.id)
        return memory, chain, events

    async def update(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        memory_id: uuid.UUID,
        payload: MemoryUpdate,
    ) -> Memory:
        """用户编辑记忆（部分更新 + 审计 + 尽力刷新向量）。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            memory_id: 记忆 ID。
            payload: 编辑字段（expires_at 出现在 model_fields_set 视为显式提交，
                传 null 清除 TTL——区分"未提交"与"提交 null"）。

        Returns:
            更新后的 Memory（未提交，由路由层依赖统一 commit）。
        """
        memory = await self._require_memory(db, ws_id=ws_id, memory_id=memory_id)
        embedding: list[float] | None = None
        if payload.content is not None:
            client = await self._get_embedding()
            if client is not None:
                try:
                    embedding = (await client.embed([payload.content]))[0]
                except EmbeddingError as exc:
                    logger.warning("memory_edit_embedding_degraded id=%s error=%s", memory_id, exc)
        await memory_repo.edit_by_user(
            db,
            memory,
            content=payload.content,
            confidence=payload.confidence,
            importance=payload.importance,
            expires_at=payload.expires_at,
            expires_at_set="expires_at" in payload.model_fields_set,
            embedding=embedding,
        )
        # flush 触发的 UPDATE 使 server 侧维护的 updated_at 过期，响应序列化
        # （Pydantic 同步上下文）访问过期属性会触发 lazy IO（MissingGreenlet），
        # 返回前统一刷新重载全部列。
        await db.refresh(memory)
        return memory

    async def delete(self, db: AsyncSession, *, ws_id: uuid.UUID, memory_id: uuid.UUID) -> Memory:
        """软删除记忆（status=deleted + 审计；检索与列表不再可见）。"""
        memory = await self._require_memory(db, ws_id=ws_id, memory_id=memory_id)
        await memory_repo.soft_delete(db, memory)
        await db.refresh(memory)  # 同 update：写路径统一刷新，防序列化 lazy IO
        return memory

    async def resolve(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        memory_id: uuid.UUID,
        keep: str,
    ) -> Memory:
        """冲突裁决：保留本条（this）或对手方（other），另一条 superseded。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace。
            memory_id: 裁决指向的记忆 ID。
            keep: "this" 保留本条 / "other" 保留同 key 另一条 conflicted。

        Returns:
            裁决后的保留条目（active，含版本链指针）。

        Raises:
            AppError: 422 —— 目标条目不处于 conflicted 状态或无对手方。
        """
        memory = await self._require_memory(db, ws_id=ws_id, memory_id=memory_id)
        if memory.status != MemoryStatus.CONFLICTED:
            raise AppError("invalid_operation", 422, "仅 conflicted 记忆可裁决")
        peer = await memory_repo.find_conflict_peer(db, memory)
        if peer is None:
            raise AppError("invalid_operation", 422, "不存在同 key 的冲突对手方")
        winner, loser = (memory, peer) if keep == "this" else (peer, memory)
        await memory_repo.resolve_conflict(db, winner, loser)
        await db.refresh(winner)  # 同 update：防 updated_at 过期引发的 lazy IO
        return winner

    async def search(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        payload: MemorySearchRequest,
    ) -> list[ScoredMemory]:
        """检索测试（面板"搜索测试"）：与对话链路同一检索器与打分。

        Returns:
            ScoredMemory 列表（score 降序）；embedding 不可用时空列表。
        """
        retriever = await self._get_retriever()
        if retriever is None:
            return []
        return await retriever.retrieve(
            db,
            ws_id=ws_id,
            user_id=user_id,
            query=payload.query,
            top_k=payload.top_k,
        )
