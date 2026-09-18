"""记忆检索（M-04 读路径，docs/01 §5.2.3）。

流程：query 向量化 → 向量召回（active/未过期/归属过滤）→ 加权重排
（sim*0.5 + importance*0.2 + recency*0.15 + hit*0.15）→ 同 key 去重
→ top-k → 命中写回 hit_count 与 HIT 事件。

Embedding 失败时降级返回空列表（检索是增强项，不阻塞回答主链路）。
"""

import logging
import math
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.embeddings import EmbeddingClient, EmbeddingError
from app.memory.schemas import ScoredMemory
from app.repositories import memory_repo

logger = logging.getLogger(__name__)

# 加权重排的权重（docs/06 §M-04 检索接口约定）
_W_SIMILARITY = 0.5
_W_IMPORTANCE = 0.2
_W_RECENCY = 0.15
_W_HIT = 0.15
# recency 衰减：updated_at 距今 N 天的线性衰减半周期（天）
_RECENCY_HALF_LIFE_DAYS = 30.0
# hit_count 归一化的饱和常数（约 20 次命中视为满分）
_HIT_SATURATION = 20.0


def _recency_score(updated_at: datetime | None) -> float:
    """时间近度得分：最近更新的记忆接近 1，随半周期 30 天指数衰减。"""
    if updated_at is None:
        return 0.0
    now = datetime.now(UTC)
    updated = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
    days = max((now - updated).total_seconds() / 86400.0, 0.0)
    return math.exp(-days / _RECENCY_HALF_LIFE_DAYS)


class MemoryRetriever:
    """长期记忆检索器（供 Context Builder / 记忆面板调用）。"""

    def __init__(self, embedding: EmbeddingClient) -> None:
        """注入 Embedding 客户端（测试可替换 fake）。

        Args:
            embedding: OpenAI 兼容 /embeddings 客户端。
        """
        self._embedding = embedding

    async def retrieve(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        user_id: uuid.UUID,
        query: str,
        top_k: int | None = None,
    ) -> list[ScoredMemory]:
        """检索与 query 最相关的长期记忆。

        Args:
            db: 数据库会话。
            ws_id: 所属 workspace（隔离边界）。
            user_id: 归属用户。
            query: 当前用户问题（向量化后检索）。
            top_k: 返回条数；None 用配置默认值。

        Returns:
            综合分降序的记忆列表；embedding 不可用时返回空列表。
        """
        settings = get_settings()
        try:
            vectors = await self._embedding.embed([query])
        except EmbeddingError as exc:
            logger.warning("retrieve_degraded_no_embedding ws=%s error=%s", ws_id, exc)
            return []
        query_vector = vectors[0]

        candidates = await memory_repo.search_candidates(
            db,
            ws_id=ws_id,
            user_id=user_id,
            embedding=query_vector,
            candidate_k=settings.retrieval_candidate_k,
        )

        # ---- 加权重排 + 同 key 只保留最高分 ----
        best_by_key: dict[str, ScoredMemory] = {}
        for memory, similarity in candidates:
            score = (
                similarity * _W_SIMILARITY
                + memory.importance * _W_IMPORTANCE
                + _recency_score(memory.updated_at) * _W_RECENCY
                + min(memory.hit_count / _HIT_SATURATION, 1.0) * _W_HIT
            )
            scored = ScoredMemory(memory=memory, similarity=similarity, score=score)
            current = best_by_key.get(memory.key)
            if current is None or scored.score > current.score:
                best_by_key[memory.key] = scored

        results = sorted(best_by_key.values(), key=lambda s: s.score, reverse=True)
        selected = results[: top_k or settings.retrieval_top_k]

        # ---- 命中写回（hit_count 与 HIT 事件，commit 由调用方统一）----
        await memory_repo.record_hits(db, [s.memory.id for s in selected])
        return selected
