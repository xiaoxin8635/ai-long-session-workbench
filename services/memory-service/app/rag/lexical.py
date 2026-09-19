"""RAG 词法检索路（M-07 BM25，docs/06 §9 混合检索的关键词分支）。

索引侧：jieba 搜索模式分词，ingest 时随 chunk 存 tokens JSONB。
查询侧：对该 workspace 全部在库 chunk 的 tokens 现场建 BM25Okapi 索引打分
（个人工作台规模 <1 万 chunk，构建耗时百毫秒级；索引缓存留作后续优化点）。
"""

import uuid

from rank_bm25 import BM25Okapi


def rank_by_bm25(
    corpus: list[tuple[uuid.UUID, list[str]]], query_tokens: list[str], top_k: int
) -> list[tuple[uuid.UUID, float]]:
    """BM25 打分并返回 top-k。

    Args:
        corpus: (chunk_id, 预分词 tokens) 列表（workspace 内在库切片）。
        query_tokens: 查询分词结果（tokenize_for_index 产出，口径一致）。
        top_k: 返回条数。

    Returns:
        (chunk_id, bm25 分) 列表，分数降序；语料或查询为空返回空列表。
    """
    if not corpus or not query_tokens:
        return []
    # 过滤空 tokens 条目：空文档参与索引会拉低平均长度，全空语料直接除零
    scored = [(cid, tokens) for cid, tokens in corpus if tokens]
    if not scored:
        return []
    # 语料平滑：附加两个空文档抬高 N，根治小语料 idf 退化——idf>0 要求
    # N > 2*freq，最坏时查询词命中全部真实文档（freq=N_real），故加 2 恒够
    # （rank_bm25 已知行为；空文档对查询词 tf=0 恒得 0 分，不会进入返回）
    index = BM25Okapi([*(tokens for _, tokens in scored), [], []])
    scores = index.get_scores(query_tokens)[: len(scored)]
    ranked = sorted(
        ((cid, float(score)) for (cid, _), score in zip(scored, scores, strict=True)),
        key=lambda x: x[1],
        reverse=True,
    )
    return [(cid, score) for cid, score in ranked[:top_k] if score > 0]
