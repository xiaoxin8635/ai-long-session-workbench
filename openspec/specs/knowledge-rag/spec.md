## Purpose

知识库把上传文档解析为中文友好的重叠切片，并以 BGE-M3 稠密向量与 jieba BM25 分词双路入索引；检索走 RRF 融合加可选 rerank 精排，产出可定位到 chunk 的引用。任意一路依赖不可用都 MUST 静默降级，不阻断对话主链路。

## Requirements

### Requirement: upload-validation-and-parsing
POST /api/knowledge/files SHALL 以 multipart 接收单文件：字节数超过 `MEMORY_SERVICE_RAG_MAX_UPLOAD_MB`（默认 20）MB 返回 413（`knowledge_file_too_large`，文案含「MB 上限」）；扩展名限于 pdf/docx/md/txt/csv/xlsx，其余返回 422（`knowledge_parse_error`）；空字节、加密 PDF、无文本层扫描件、docx/csv 无有效数据行、md/txt 非 UTF-8 均返 422；md/txt 按 utf-8-sig 解码，csv 依次尝试 utf-8-sig→gbk→latin-1，表格类渲染为「表头 + 逐行 `列名: 值`」且每表截断在 5000 行；成功返回 201 且 status 为 `parsing`。

#### Scenario: unsupported-extension-rejected
- GIVEN 已登录且为目标 workspace 成员的用户
- WHEN 上传扩展名为 exe 的文件
- THEN 响应状态码为 422 且错误码为 `knowledge_parse_error`
- AND knowledge_files 表未新增记录

#### Scenario: oversized-file-rejected
- GIVEN `MEMORY_SERVICE_RAG_MAX_UPLOAD_MB` 为 20
- WHEN 上传约 21MB 的 pdf
- THEN 响应状态码为 413 且错误码为 `knowledge_file_too_large`

### Requirement: checksum-dedup-and-version-chain
系统 SHALL 按 workspace + SHA-256 checksum 判重：命中同 checksum 文件时返回已有文件与切片数（不新建记录），且该文件处于 `parsing` 或 `failed` 时重派摄取；未命中且存在同名非 superseded 文件时 MUST 令新文件 version = 旧 version + 1 并把旧版本置 `superseded`（切片 embedding 置 NULL、tokens 置空数组，正文保留），使双路检索均不再召回旧版。

#### Scenario: identical-content-reupload-is-idempotent
- GIVEN 已上传过 same.txt
- WHEN 再次上传字节完全相同的同名文件
- THEN 两次响应均为 201 且返回的 id 相同
- AND GET /api/knowledge/files 仍只列出一条记录

#### Scenario: same-filename-supersedes-old-version
- GIVEN guide.md 的 v1 已存在
- WHEN 上传内容不同的同名 guide.md
- THEN 新记录 version 为 2 且旧记录 status 转为 superseded
- AND 检索旧版关键词只命中 v2 的切片

### Requirement: ingest-state-machine-and-background-retry
上传请求内 SHALL 同步完成解析、切片、分词并落库（status=`parsing`），提交后 fire-and-forget 派发摄取协程：按 `MEMORY_SERVICE_EMBEDDING_BATCH_SIZE`（默认 16）分批调用 OpenAI 兼容 `POST {embedding_base_url}/embeddings`（模型默认 `BAAI/bge-m3`、维度 1024、超时 30 秒、5xx 与网络错误重试 1 次并线性退避）回填 embedding 后把文件置 `embedded`；异常时以独立会话把仍为 `parsing` 的文件置 `failed`；文件已删除或状态非 `parsing` 时 MUST 静默放弃。

#### Scenario: successful-ingest-reaches-embedded
- GIVEN 文件处于 parsing 且有若干待补向量切片
- WHEN 后台摄取任务执行完毕
- THEN 全部切片 embedding 非空且文件 status 变为 embedded
- AND 再次取 pending 切片为空列表

#### Scenario: embedding-failure-marks-failed
- GIVEN embedding 服务重试后仍返回 5xx
- WHEN 后台摄取任务执行
- THEN 文件 status 变为 failed 且不外抛调用方可见异常
- AND 重传同内容文件会重新派发摄取

### Requirement: chunking-and-tokenization
切片 SHALL 用 `split_chunks`：目标 token 取 `MEMORY_SERVICE_RAG_CHUNK_TOKENS`（默认 512）、重叠比例取 `MEMORY_SERVICE_RAG_CHUNK_OVERLAP_RATIO`（默认 0.1）；按空行分段后贪心合并至软上限，markdown 标题行（`#` 开头）强制开新块且不带上一块 overlap，超过目标 token 的单段先按中英文句末标点切句再参与贪心，无标点极端长句按 max(target, 64) 字符滑窗硬切；每片 MUST 落 `chunk_index`、`token_count` 与 jieba 搜索模式分词（过滤空白与纯标点）后的 `tokens`，且 MUST 与查询侧分词同口径。

#### Scenario: heading-starts-new-block-without-overlap
- GIVEN 文本为「正文甲\n\n# 第二章标题\n\n第二章正文」
- WHEN 以 overlap_ratio 为 0.5 切片
- THEN 产出两块且第二块以 `# 第二章标题` 开头
- AND 第二块不含上一块的尾部段落

#### Scenario: long-paragraph-sentence-split
- GIVEN 一段无空行且远超目标 token 的文本
- WHEN 以 target_tokens 为 40 切片
- THEN 产出多块且各块非空
- AND 各块拼接后原文未丢失

### Requirement: hybrid-retrieval-rrf-and-rerank
检索SHALL 双路召回后融合：向量路按 pgvector 余弦相似度（1 − cosine_distance，自动排除 embedding 为 NULL 的切片）取 `MEMORY_SERVICE_RAG_CANDIDATE_K`（默认 40）条；BM25 路对 workspace 内 tokens 非空切片现场建 BM25Okapi 索引（附加两个空文档做语料平滑）取同数量候选；RRF 融合 score = Σ 1/(60 + rank)，rank 自 0 起；取融合头部 max(top_k×3, 20) 条送 rerank（模型默认 `BAAI/bge-reranker-large`，base_url 缺省复用 embedding 服务），可用则按精排分排序，否则按 RRF 分直排；最终返回 `MEMORY_SERVICE_RAG_TOP_K`（默认 6）条带 score 与 vector_similarity 的 CitedChunk，两路皆空返回空列表。

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

### Requirement: graceful-degradation-without-embeddings
`MEMORY_SERVICE_EMBEDDING_BASE_URL` 未配置或调用失败时，服务层 SHALL 把 embedding 客户端降为 None 并退化为纯 BM25 检索（命中项 vector_similarity 为 0.0），日志记录 `rag_vector_degraded`；`MEMORY_SERVICE_RERANK_ENABLED` 为 false（CPU 部署默认关闭）时 MUST 恒走 RRF 直排；检索链路 MUST 不向调用方抛异常。

#### Scenario: bm25-only-when-embedding-unavailable
- GIVEN embedding 未配置而切片 tokens 已就绪
- WHEN 以「知识库混合检索架构」检索
- THEN 返回相关性最高的切片且 vector_similarity 为 0.0
- AND 响应码为 200

#### Scenario: superseded-version-excluded-from-both-paths
- GIVEN 旧版本已被置 superseded（tokens 清空、embedding 置 NULL）
- WHEN 以旧版正文检索
- THEN 结果不含旧版切片
- AND 同文件新版本切片仍正常命中

### Requirement: citations-and-attachment-injection
装入 RAG 区块的切片 SHALL 出现在 chat 流式 finish 片的 `metadata.citations`（chunk_id/file_id/filename/chunk_index/score），超预算未装入的命中 MUST 不进 citations；本轮附件经 `fetch_by_file_ids` 全文直注并赋 FORCED_ATTACHMENT_SCORE=2.0 以优先装入并置顶 citations；不属于本 workspace 的附件 MUST 静默跳过；DELETE /api/knowledge/files/{id} SHALL 硬删文件与级联切片并返回 204，重复删除返回 404 且删除后检索不再命中。

#### Scenario: budget-trimmed-hits-not-cited
- GIVEN 两条各 550 token 的命中而 RAG 预算仅够装入一条
- WHEN 完成一轮对话并取 finish 片
- THEN citations 只有 1 条
- AND system 正文只含被装入的那条内容

#### Scenario: delete-removes-document-from-retrieval
- GIVEN 已上传 temp.md 且检索可命中
- WHEN 删除该文件后再次检索同一关键词
- THEN DELETE 返回 204 且检索 hits 为空列表
- AND 再次 DELETE 同一 id 返回 404
