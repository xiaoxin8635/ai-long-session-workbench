# Design

## Context

见 proposal.md 的 Why。补充实现约束：

- 现有仓储层刻意采用「免 join」设计——`knowledge_chunks` 冗余 `workspace_id`，supersede 时通过 **双清 chunk 行的检索键**（`embedding=NULL`、`tokens=[]`）让两路检索自然下线，模块 docstring 明确写着「无需回查 knowledge_files.status」。该假设对 superseded 成立，对 failed 不成立。
- 已核实的库表条件：`knowledge_chunks` 上有 `ix_knowledge_chunks_file (file_id, chunk_index)`，`knowledge_files` 上有 `ix_knowledge_files_ws (workspace_id, status)`，两者都为状态关联查询做好了铺垫，无需新增迁移索引。
- 现有回归锚点：`tests/test_rag_pipeline.py::test_retriever_bm25_only_and_superseded_excluded` 已以「旧版 tokens 清空」的形态覆盖 superseded 不可召回；本次沿用同一构造手法覆盖 failed。

## Goals / Non-Goals

**Goals:**

- 让文件生命周期状态成为检索可见性的唯一权威判据，消除对「某种状态恰好清理了 chunk 键」这一隐式耦合的依赖。
- 使 failed 与 superseded 两条下线路径的行为严格对称。
- 保证 `parsing` 状态文件的降级可用性不退化（摄取进行中仍可词法召回）。

**Non-Goals:**

- 不改写历史数据（不批量清理已 failed 文件的 tokens），仅约束查询可见性。
- 不引入自动重试排队或失败文件的后台修复队列。
- 不改变上传 / 删除 / 引用 / cited 机制本身的契约。

## Decisions

### Decision 1：可见性判据以 knowledge_files.status 为准，采用「显式排除集」

在 `vector_search` 与 `load_corpus_tokens` 中 join `knowledge_files`，排除 status ∈ {`failed`, `superseded`} 的切片。

- **备选 A：白名单（只放行 parsing/embedded）** —— 将来新增未知状态时默认不可召回更安全，但会静默吞掉合法新状态的可见性，且与「changed 未定义即拒绝」相比更易造成误伤。
- **备选 B：继续走 chunk 行自身键 + 在 _mark_failed 里补清** —— 改动最小、零 join，但契约仍隐藏在「每个终态都要记得清 key」的约定里，第三次新增状态时大概率复现同类缺陷。
- **选择**：排除集 + JOIN 查询，把契约收敛到单点。同时保留 Decision 2 的对称性修复，使两层互相兜底。

### Decision 2：_mark_failed 与 mark_superseded 行为对齐

`_mark_failed` 在把 status 改为 `failed` 的同一次事务内执行一次 `update(KnowledgeChunk).values(embedding=None, tokens=[])`。

理由：即便 Decision 1 已在查询侧把关，双清仍有必要——它保证「列表显示失败」与「底层数据已下线」语义一致，也让依赖仓储层其它出口（如未来新增的导出/统计路径）不会踩到同一坑。两者不是重复劳动，而是「防御纵深」与「数据一致性」各司其职。

注意 `_mark_failed` 使用独立会话（原会话可能已随异常失效），清理与状态改写必须在同一会话、同一 commit 内，不得拆成两次提交。

### Decision 3：不新增数据库迁移

`file_id` 与 `workspace_id+status` 复合索引均已就位（见 Context），本次只改查询与写入逻辑，不涉及 schema 变更。若后续性能观测显示 join 成为瓶颈，再考虑物化 status 冗余列到 chunk 行。

## Risks / Trade-offs

- **全量语料加载的 join 开销**：`load_corpus_tokens` 每次检索都全量加载 workspace 语料。→ Mitigation：join 走 `ix_knowledge_chunks_file`，且 `knowledge_files` 相对 chunks 行数极小，规划器会选择 hash join；上线后以探针计时的 RAG 检索耗时做前后对比，无显著劣化即可接受。
- **历史 failed 文件的正文仍在库**：查询侧已过滤，不构成泄漏风险（同 workspace 隔离仍由 `workspace_id` 保证）。→ Mitigation：切片正文保留是有意为之，供失败诊断与重传判定。
- **极端时序：查询时正处于 failed 迁移事务中** → 事务隔离保证读到迁移前的快照（旧态可见），属可接受窗口；后续查询即收敛。
- **双 spec 的职责边界**：新增能力 `knowledge-retrieval-visibility` 与既有 `knowledge-rag` 存在语义重叠。→ Mitigation：前者持有「状态—可见性映射」契约，后者保留「召回与融合算法」契约，archive 后两者并存，避免 knowledge-rag 的 requirement 过度膨胀。

## Migration Plan

1. 改 `app/repositories/knowledge_repo.py`：`vector_search` 与 `load_corpus_tokens` 增加 join 与状态排除。
2. 改 `app/rag/ingest.py`：`_mark_failed` 增加同事务双清。
3. 同步修订 knowledge_repo 模块 docstring 中「免 join 设计 / 无需回查 status」的过期说明。
4. 补测试并跑全量：加 `test_retriever_excludes_failed_files` 与 `_mark_failed` 双清用例。
5. 重启 memory-service 使代码生效；用线上已 failed 的 `面试高频问题手册.md` 做实机复验：检索其关键词应零命中，对话 citations 不含该文件。
6. **回滚**：本次无 schema 变更，回滚即为还原两处查询/写入逻辑，数据无需修复。

## Open Questions

无。
