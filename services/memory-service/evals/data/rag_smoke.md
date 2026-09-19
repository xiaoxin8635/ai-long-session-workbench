# EchoDesk RAG 冒烟评测文档（M-07 Recall@K 数据集）

用于本地链路召回率自测：每段一个独立主题，检索查询与段落一一对应
（见同目录 rag_queries.jsonl）。段落间空行分隔，保证 chunker 段落感知。

## 部署架构

EchoDesk 采用 Open WebUI 作为产品壳，自研 memory-service 承载四层记忆、上下文组装与 RAG 检索。服务间通过 Docker Compose 编排，PostgreSQL 同时承担关系数据与向量存储（pgvector 扩展）。

## 混合检索策略

知识检索采用双路混合：向量召回使用 bge-m3 模型计算余弦相似度，关键词召回使用 jieba 分词后建立的 BM25 索引。两路结果经 RRF 倒数排名融合，再由 bge-reranker 精排出最终顺序。

## 上下文预算

上下文装配按区块预算裁剪：system、procedural、semantic、episodic、working、rag、tool_results 各占比例由 YAML profile 定义，全局超限时按 evict_order 逐块放弃，working 区块始终保留最后一条用户消息。

## 记忆分层

长期记忆分为三个类型：procedural 记录用户偏好与工作流，semantic 记录事实类信息，episodic 记录会话摘要。冲突条目进入 conflicted 状态，由用户在记忆面板裁决保留方。

## 滚动摘要机制

每轮对话结束后，当会话消息累计超过阈值时触发摘要压缩：滑出窗口的历史消息被并入会话滚动摘要，摘要同时沉淀为 episodic 记忆供跨会话检索。

## 用量统计

token 用量按轮次落库，记录 prompt 与 completion 分解，以及各记忆区块的实际注入量（memory、rag、tool），为看板提供总量、占比与按日趋势数据。

## 安全与鉴权

全部 API 走 Bearer JWT 认证，workspace 为隔离边界：非成员访问统一返回 403 防枚举。密钥只存于 .env 文件，配置模板见 .env.example，数据库操作一律走 ORM 参数化查询。

## 文档摄取管线

上传文件在请求内同步完成解析（pdf/docx/md/txt）、段落感知切片与 jieba 分词；embedding 向量化由后台任务异步补齐，状态机从 parsing 推进到 embedded，失败进入 failed 可重试。同名重传自动递增版本号，旧版向量下线但文本保留可追溯。
