# Proposal

## Why

知识摄取失败的文件仍可被对话检索引用。上传请求在同步阶段就把切片落库（含 jieba 分词 tokens，embedding 留空），后台摄取失败时 `_mark_failed` 只把 `knowledge_files.status` 改为 `failed`，却不像 `mark_superseded` 那样清空切片的 `tokens`/`embedding`；而 BM25 语料加载 `load_corpus_tokens` 的过滤条件只看 `tokens` 非空、从不回查 `knowledge_files.status`，于是「摄取失败」与「检索可见」脱钩。实测线上已出现该状态：`面试高频问题手册.md` 处于 `failed` 且 0 条向量，却持有 60 条非空 tokens 切片，仍被词法检索召回到对话的 citations 中。

## What Changes

- 检索层把关：`vector_search` 与 `load_corpus_tokens` 关联 `knowledge_files`，统一排除 `FAILED` 与 `SUPERSEDED` 状态文件的切片，使「文件生命周期状态」成为检索可见性的唯一判据，将来新增任意终态都不会再漏。
- 状态迁移对称化：`_mark_failed` 在置 `failed` 时同步执行与 `mark_superseded` 相同的检索键双清（embedding 置 NULL、tokens 置空数组），保证两处下线路径行为一致。
- 保留既有语义：失败文件在列表与详情中仍可见其状态与切片正文（可追溯、可诊断），仅从检索语料中下线。

## Capabilities

### New Capabilities

- `knowledge-retrieval-visibility`：文件生命周期状态（parsing/failed/embedded/superseded）与双路检索可见性之间的统一映射契约，规定哪些状态 MUST 可被召回、哪些 MUST 不可被召回。

### Modified Capabilities

- `knowledge-rag`：新增「失败文件不可被检索」的行为要求，并修订既有下线机制的措辞——由「依赖 chunk 行自身状态」改为「以 knowledge_files.status 为判据」。

## Impact

- `services/memory-service/app/repositories/knowledge_repo.py`：`vector_search`、`load_corpus_tokens` 增加 join `knowledge_files` 与状态排除条件；仓储模块 docstring 的「免 join 设计」说明需同步修订。
- `services/memory-service/app/rag/ingest.py`：`_mark_failed` 增加检索键双清逻辑，与 `mark_superseded` 对齐。
- `services/memory-service/tests/test_rag_pipeline.py`：新增失败文件与 superseded 文件双路径不可召回的回归用例。
- 性能影响：`load_corpus_tokens` 每次检索全量加载 workspace 语料，增加一次 join；需确认 `knowledge_chunks.file_id` 上的索引存在，避免大知识库下退化为全表扫描。
- 数据影响：本变更仅约束查询可见性，不改写已有切片数据；历史 `failed` 文件的正文与 tokens 保留在库中，重新上传同内容文件仍可命中 checksum 幂等分支并重新派发摄取。
