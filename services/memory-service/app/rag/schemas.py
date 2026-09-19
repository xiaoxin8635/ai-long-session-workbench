"""RAG 域数据结构（M-07）。

CitedChunk：检索单元（chunk 定位 + 双路分数），供注入 RAG 区块与组装引用。
"""

import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CitedChunk:
    """带引用定位的检索命中片段。

    Attributes:
        chunk_id: 切片数据库 ID。
        file_id: 所属文件 ID。
        filename: 文件名（引用展示与溯源）。
        chunk_index: 文件内切片序号（0 起，与原文位置对应）。
        content: 片段正文。
        score: 综合分（rerank 分数；rerank 不可用时为 RRF 融合分）。
        vector_similarity: 向量路余弦相似度（该路未命中为 0.0）。
    """

    chunk_id: uuid.UUID
    file_id: uuid.UUID
    filename: str
    chunk_index: int
    content: str
    score: float
    vector_similarity: float = 0.0
