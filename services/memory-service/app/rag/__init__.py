"""RAG 知识库域（M-07，docs/01 §5.4 / docs/06 §9）。

摄入：解析(pdf/md/docx/txt) → 段落感知切片 → jieba 分词 + embedding → pgvector 入库。
检索：向量召回 + BM25 关键词召回 → RRF 融合 → rerank 精排 → CitedChunk（引用定位）。
"""
