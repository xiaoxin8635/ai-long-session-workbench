## Purpose

四层记忆（Working / Episodic / Semantic / Procedural）的存储归属与过期策略，以及记忆的写路径（抽取 → 去重 → 冲突仲裁 → 入库）与读路径（向量召回 → 加权重排 → 命中写回）的既有行为契约，含 memory_events 溯源流水。

## Requirements

### Requirement: four-layer-storage-and-expiry
系统 SHALL 按层区分存储：Working 存 Redis list 键 `wm:session:<session_id>`（每会话一键，TTL 7 天即 604800 秒，追加后 LTRIM 只保留最近 200 条）；Episodic / Semantic / Procedural 落 PG `memories` 表（`memory_type` 区分，1024 维 `embedding` 列）；Knowledge 存向量库 knowledge_chunks，不落 memories 表。过期 MUST 为软过期：`expires_at` 为空或晚于当前时刻的条目才可被检索，不做物理删除；抽取事实带 `ttl_days` 时 `expires_at` = 当前时刻 + ttl_days 天。

#### Scenario: working-window-append-keeps-bounded-tail
- GIVEN 一个会话已追加 200 条以上消息
- WHEN 再追加一条消息
- THEN Redis 键 `wm:session:<session_id>` 只保留最近 200 条
- AND 该键的 TTL 被重置为 604800 秒

#### Scenario: expired-memory-not-retrievable
- GIVEN 一条 `expires_at` 为 1 小时前的 semantic 记忆与一条未过期的近邻记忆
- WHEN 以与过期条目同向的向量检索
- THEN 结果中不含该过期条目
- AND 未过期的次近邻条目仍被返回

### Requirement: extraction-pipeline-and-degradation
写路径 SHALL 由 ExtractPipeline 编排：注入 workspace 内 active distinct key（上限 64，按最近更新排序）引导复用命名 → LLM 结构化抽取（最近 20 条消息，带 `[message_id]` 标注）→ 丢弃 `confidence` 低于 `extract_min_confidence`（默认 0.5）的候选 → 溯源后验过滤 → 批量向量化 → 逐条去重与仲裁入库。抽取 LLM 失败或输出非法时 MUST 跳过本轮并返回空列表（不写库、不调用向量化）；向量化失败时 SHALL 降级为无向量入库并只走 key 精确判重。

#### Scenario: extract-llm-failure-degrades-to-noop
- GIVEN 抽取 LLM 抛出 LLMError
- WHEN 执行一轮抽取入库
- THEN 返回空列表且库内无新增记忆
- AND embedding 客户端零调用

#### Scenario: low-confidence-fact-dropped
- GIVEN 抽取结果中有一条 `confidence` 为 0.2 的候选
- WHEN 执行抽取入库
- THEN 该候选被丢弃，未写入 memories 表

### Requirement: traceability-postfilter
系统 MUST 对抽取候选做溯源后验过滤：候选的 `source_message_ids` 必须非空且至少一条指向本轮真实消息；全部引用非法则该候选丢弃；部分非法时剔除非法引用、保留合法部分入库。

#### Scenario: untraceable-candidates-dropped
- GIVEN 抽取结果含空引用候选与引用编造消息 ID 的候选各一条，另有合法引用候选一条
- WHEN 执行抽取入库
- THEN 仅合法引用候选入库
- AND 空引用与编造引用的候选均不落库

#### Scenario: mixed-refs-stripped-to-valid
- GIVEN 一条候选的 `source_message_ids` 为 [编造 ID, 本轮真实消息 ID]
- WHEN 执行抽取入库
- THEN 该条入库且其 `source_message_ids` 仅保留本轮真实消息 ID

### Requirement: three-tier-dedup
MemoryDeduplicator SHALL 按三层顺序判重并在首次命中时返回：① workspace + key 精确匹配 active 条目（similarity=1.0，strict=true）；② 向量余弦相似度 ≥ `dedup_similarity_threshold`（默认 0.92）的 active 条目（strict=true，允许 MERGE）；③ 相似度 ≥ `conflict_similarity_threshold`（默认 0.80）但未达判重阈值的疑似冲突层（strict=false）。无向量时仅走第 ① 层；三层均未命中则新建条目。

#### Scenario: exact-key-hit-is-strict
- GIVEN 库中存在同 workspace 同 key 的 active 记忆
- WHEN 对同 key 的候选事实判重
- THEN 返回该旧条目且 similarity 为 1.0、strict 为 true
- AND 不再进行向量近邻查询

#### Scenario: suspect-layer-hit-is-non-strict
- GIVEN 候选向量与库内 active 条目余弦相似度为 0.85
- WHEN 判重
- THEN 返回该条目且 strict 为 false
- AND 仲裁结论为 MERGE 时被降级为 COEXIST，旧条正文不被改写

### Requirement: conflict-arbitration-actions
ConflictResolver SHALL 由 LLM 仲裁并映射为落库动作：MERGE（仅 strict 命中允许，更新 content/confidence、`version` 自增、不新建条目）、SUPERSEDE（新条挂 `supersedes_id`、旧条置 superseded）、COEXIST（新旧双条置 conflicted 待用户裁决）、INDEPENDENT（双条保持 active、不挂链、不打冲突标）。LLM 调用失败或输出非法时 MUST 降级为 COEXIST（保守不丢信息）。

#### Scenario: supersede-builds-version-chain
- GIVEN 同 key 旧条目存在且仲裁返回 supersede
- WHEN 新事实入库
- THEN 新条 status 为 active 且 `supersedes_id` 指向旧条
- AND 旧条 status 变为 superseded 并记录 SUPERSEDE 事件

#### Scenario: independent-keeps-both-active
- GIVEN 仲裁返回 independent
- WHEN 新事实入库
- THEN 新旧两条均保持 active 且新条 `supersedes_id` 为空
- AND 两条均不产生 CONFLICT 或 SUPERSEDE 事件

### Requirement: retrieval-ranking-and-hit-writeback
MemoryRetriever SHALL 按如下契约检索：query 向量化失败返回空列表；候选过滤 workspace + user 归属、状态为 active 或 conflicted、类型为 semantic/episodic/procedural、未过期且向量非空，召回 `retrieval_candidate_k`（默认 50）条；综合分 = 相似度×0.6 + importance×0.2 + recency（30 天半衰期）×0.15 + min(hit_count/20, 1)×0.05，CONFLICTED 条目综合分再乘 `conflicted_retrieval_penalty`（默认 0.6）；同 key 只保留最高分；分区保底截断至 `retrieval_top_k`（默认 8），semantic 保底 top_k//2 席、procedural 取其余席位、不足时按分补位；命中写回 `hit_count`+1 与 `last_hit_at` 并记 HIT 事件，写回失败降级为告警不阻断检索。

#### Scenario: hit-writeback-and-latency
- GIVEN 库内有 50 条带向量的记忆
- WHEN 执行一次检索
- THEN 返回 8 条且耗时低于 300ms
- AND 命中条目 hit_count 为 1 且事件流含 HIT

#### Scenario: conflicted-entry-retrievable-with-penalty
- GIVEN 同相似度与 importance 的 ACTIVE 与 CONFLICTED 条目各一条
- WHEN 检索命中两者
- THEN CONFLICTED 条目排在 ACTIVE 之后且综合分更低
- AND 同 key 的多条候选中仅保留最高分一条

### Requirement: memory-events-audit
所有记忆状态变更 SHALL 写 `memory_events` 流水（event_type + old_value/new_value + source），类型覆盖 create/update/merge/conflict/supersede/hit/edit_by_user/delete；抽取管线事件 source 为 llm，滚动摘要与命中写回为 system，记忆面板用户操作为 user。

#### Scenario: create-and-hit-events-recorded
- GIVEN 一条新抽取的记忆被检索注入
- WHEN 查询其事件流水
- THEN 含 source 为 llm 的 CREATED 事件
- AND 含 source 为 system 的 HIT 事件

#### Scenario: user-adjudication-records-user-sourced-events
- GIVEN 两条同 key 的 conflicted 记忆
- WHEN 用户在面板裁决保留其中一条
- THEN 保留条目回 active 并记 UPDATED 事件、对手方置 superseded 并记 SUPERSEDE 事件
- AND 两条事件 source 均为 user
