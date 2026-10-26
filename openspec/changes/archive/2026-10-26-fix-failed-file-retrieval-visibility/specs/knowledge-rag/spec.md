# Spec Delta

## MODIFIED Requirements

### Requirement: hybrid-retrieval-rrf-and-rerank
检索SHALL 双路召回后融合：两路 MUST 先按所属文件的生命周期状态过滤，只保留 `parsing` 与 `embedded` 状态文件的切片（`failed`、`superseded` 一律排除）；向量路按 pgvector 余弦相似度（1 − cosine_distance，自动排除 embedding 为 NULL 的切片）取 `MEMORY_SERVICE_RAG_CANDIDATE_K`（默认 40）条；BM25 路对 workspace 内 tokens 非空切片现场建 BM25Okapi 索引（附加两个空文档做语料平滑）取同数量候选；RRF 融合 score = Σ 1/(60 + rank)，rank 自 0 起；取融合头部 max(top_k×3, 20) 条送 rerank（模型默认 `BAAI/bge-reranker-large`，base_url 缺省复用 embedding 服务），可用则按精排分排序，否则按 RRF 分直排；最终返回 `MEMORY_SERVICE_RAG_TOP_K`（默认 6）条带 score 与 vector_similarity 的 CitedChunk，两路皆空返回空列表。

#### Scenario: rerank-reorders-over-vector-similarity
- GIVEN 两块切片，其一向量方向正交但正文含干扰关键词
- WHEN 查询命中两者且 rerank 给干扰块满分
- THEN 返回首项为干扰块
- AND 各项 vector_similarity 为其向量路余弦相似度

#### Scenario: rerank-failure-falls-back-to-rrf
- GIVEN rerank 调用抛出 RerankError
- WHEN 执行一次检索
- THEN 结果按 RRF 融合分降序且向量近邻块居首
- AND 端点返回 200 而不报错

#### Scenario: failed-file-chunks-excluded-despite-usable-tokens
- GIVEN `面试高频问题手册.md` status 为 `failed`，持有 60 条非空 tokens 切片且 embedding 全为空
- WHEN 以该文档正文中的关键词执行检索
- THEN BM25 路与向量路候选均不含该文件的任何切片
- AND 同一 workspace 内 `embedded` 状态文件的切片召回行为不受影响
