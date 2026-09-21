"""记忆检索（M-04 读路径，docs/01 §5.2.3）。

流程：query 向量化 → 向量召回（active + conflicted/未过期/归属过滤）→ 加权重排
（sim*0.6 + importance*0.2 + recency*0.15 + hit*0.05，CONFLICTED 条目乘惩罚系数）
→ 同 key 去重 → top-k → 命中写回 hit_count 与 HIT 事件。

CONFLICTED（待用户裁决）条目参与检索但降权：完全排除会使待裁决期间的信息
从上下文静默消失（Fix A 疑似冲突降级的副作用，评测优化轮实锤）；降权使其
排在同条件 ACTIVE 之后，预算紧张时优先被裁掉。

Embedding 失败时降级返回空列表（检索是增强项，不阻塞回答主链路）。
"""

import logging
import math
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.embeddings import EmbeddingClient, EmbeddingError
from app.memory.schemas import ScoredMemory
from app.models.enums import MemoryStatus
from app.repositories import memory_repo

logger = logging.getLogger(__name__)

# 加权重排的权重（docs/06 §M-04 检索接口约定）
# hit 权重 0.15→0.05、sim 0.5→0.6（评测优化轮三期，fix_v10_mh 100 行全量实锤）：
# hit_count 是跨查询累积量，20 次即饱和满分的绝对归一让高频"万金油"条目
# 恒拿 0.15、与冷条目（hit=0）拉开 0.15 的不可翻越分差——700 次检索后
# 头部条目 hit 200~500 恒满分，sim 0.74 的目标条目以 0.006 分差出局，
# recall 崩至 0.22。sim 是每查询的主信号（权重升 0.6），hit 降为弱使用
# 反馈（满分差 0.05，sim 差可稳定翻越；同 sim 下高频条目仍优先）。
_W_SIMILARITY = 0.6
_W_IMPORTANCE = 0.2
_W_RECENCY = 0.15
_W_HIT = 0.05
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
        conflicted_penalty = get_settings().conflicted_retrieval_penalty
        for memory, similarity in candidates:
            score = (
                similarity * _W_SIMILARITY
                + memory.importance * _W_IMPORTANCE
                + _recency_score(memory.updated_at) * _W_RECENCY
                + min(memory.hit_count / _HIT_SATURATION, 1.0) * _W_HIT
            )
            # 待裁决条目降权（内容可能是待核实的旧值：可检索但不与 ACTIVE 抢排名）
            if memory.status == MemoryStatus.CONFLICTED:
                score *= conflicted_penalty
            scored = ScoredMemory(memory=memory, similarity=similarity, score=score)
            current = best_by_key.get(memory.key)
            if current is None or scored.score > current.score:
                best_by_key[memory.key] = scored

        results = sorted(best_by_key.values(), key=lambda s: s.score, reverse=True)
        selected = results[: top_k or settings.retrieval_top_k]

        # ---- 命中写回（独立事务 + 死锁防御，Fix 轮实锤后加固）----
        # 原实现 commit 由调用方统一，hit_count UPDATE 的行锁贯穿整个 LLM 生成期，
        # 与后台抽取管线的逐候选 UPDATE 交叉形成死锁（死锁三环日志实锤），chat
        # 直接 502 / preview 500。三层防御：①record_hits 内 sorted 锁序归一；
        # ②检索内立即 commit，锁窗口缩至毫秒级；③DBAPIError 降级为告警——
        # 命中统计是增强项，绝不阻断检索与对话主链路（expire_on_commit=False，
        # commit 不影响 selected 后续属性读取）。
        hit_ids = sorted(s.memory.id for s in selected)
        try:
            await memory_repo.record_hits(db, hit_ids)
            await db.commit()
        except DBAPIError as exc:
            await db.rollback()
            logger.warning("record_hits_degraded ws=%s error=%s", ws_id, exc)
        return selected
