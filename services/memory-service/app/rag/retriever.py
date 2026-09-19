"""知识库混合检索器（M-07，docs/06 §9）。

流程：query 向量化 → 向量召回（pgvector 余弦）+ BM25 召回（jieba 分词）
→ RRF 融合（score = Σ 1/(60+rank)）→ rerank 精排（bge-reranker，不可用
降级 RRF 直排）→ top-k CitedChunk。

检索是增强项：embedding 失败退化为纯 BM25，rerank 失败退化为 RRF 直排，
两路全空返回空列表——绝不抛出阻断对话主链路。
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.llm.embeddings import EmbeddingClient, EmbeddingError
from app.llm.rerank import RerankClient, RerankError
from app.rag.lexical import rank_by_bm25
from app.rag.schemas import CitedChunk
from app.rag.tokenizer import tokenize_for_index
from app.repositories import knowledge_repo

logger = logging.getLogger(__name__)

# RRF 常数（standard k=60：score = Σ 1/(k + rank)，rank 从 0 起）
RRF_K = 60


class KnowledgeRetriever:
    """RAG 知识检索器（供 Context Builder RAG 区块 / 检索调试端点共用）。"""

    def __init__(
        self, embedding: EmbeddingClient | None = None, rerank: RerankClient | None = None
    ) -> None:
        """注入可选的双路依赖（测试可替换 fake；None 时对应路自动降级）。

        Args:
            embedding: 向量化客户端；None 或调用失败退化为纯 BM25。
            rerank: 精排客户端；None 或调用失败降级 RRF 直排。
        """
        self._embedding = embedding
        self._rerank = rerank

    async def search(
        self, db: AsyncSession, *, ws_id: uuid.UUID, query: str, top_k: int | None = None
    ) -> list[CitedChunk]:
        """混合检索与 query 最相关的知识切片。

        Args:
            db: 数据库会话。
            ws_id: workspace 隔离边界。
            query: 用户问题。
            top_k: 返回条数；None 用配置默认（rag_top_k）。

        Returns:
            综合分降序的命中列表；两路均无候选返回空列表。
        """
        settings = get_settings()
        top_k = top_k or settings.rag_top_k
        candidate_k = settings.rag_candidate_k

        # ---- 向量路（失败降级为纯 BM25）----
        vector_sims: dict[uuid.UUID, float] = {}
        if self._embedding is not None:
            try:
                query_vector = (await self._embedding.embed([query]))[0]
            except EmbeddingError as exc:
                logger.warning("rag_vector_degraded ws=%s error=%s", ws_id, exc)
            else:
                vector_sims = {
                    chunk.id: sim
                    for chunk, sim in await knowledge_repo.vector_search(
                        db, ws_id=ws_id, embedding=query_vector, limit=candidate_k
                    )
                }

        # ---- BM25 路（workspace 全量在库切片现场建索引）----
        corpus = await knowledge_repo.load_corpus_tokens(db, ws_id=ws_id)
        bm25_scores = dict(rank_by_bm25(corpus, tokenize_for_index(query), candidate_k))

        if not vector_sims and not bm25_scores:
            return []

        # ---- RRF 融合（每路名次贡献 1/(60+rank)，双路命中累加）----
        rrf: dict[uuid.UUID, float] = {}
        for hits in (vector_sims, bm25_scores):
            for rank, cid in enumerate(sorted(hits, key=hits.get, reverse=True)):
                rrf[cid] = rrf.get(cid, 0.0) + 1.0 / (RRF_K + rank)

        # ---- 精排候选截断（rerank 输入限制在 RRF 头部，控制成本）----
        merged = sorted(rrf, key=rrf.get, reverse=True)
        rerank_k = max(top_k * 3, 20)
        candidate_ids = merged[:rerank_k]
        rows = {
            chunk.id: (chunk, filename)
            for chunk, filename in await knowledge_repo.fetch_by_ids(db, candidate_ids)
        }

        # ---- rerank 精排（不可用降级 RRF 直排）----
        order: list[tuple[uuid.UUID, float]]
        if self._rerank is not None and len(candidate_ids) > 1:
            try:
                scores = await self._rerank.rerank(
                    query, [rows[cid][0].content for cid in candidate_ids]
                )
            except RerankError as exc:
                logger.warning("rag_rerank_degraded error=%s", exc)
                order = [(cid, rrf[cid]) for cid in candidate_ids]
            else:
                order = list(zip(candidate_ids, scores, strict=True))
                order.sort(key=lambda x: x[1], reverse=True)
        else:
            order = [(cid, rrf[cid]) for cid in candidate_ids]

        return [
            CitedChunk(
                chunk_id=cid,
                file_id=rows[cid][0].file_id,
                filename=rows[cid][1],
                chunk_index=rows[cid][0].chunk_index,
                content=rows[cid][0].content,
                score=score,
                vector_similarity=vector_sims.get(cid, 0.0),
            )
            for cid, score in order[:top_k]
            if cid in rows
        ]
