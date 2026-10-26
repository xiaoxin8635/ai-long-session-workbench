## Purpose

上下文组装：把滚动摘要、长期记忆检索、任务简报、RAG 片段与 Working Memory 窗口等候选，在 token 预算内按固定区块顺序裁剪并渲染为可溯源 messages，同时提供只读预览端点。

## Requirements

### Requirement: fixed-section-order-and-rendering
装配与渲染顺序 SHALL 固定为 system → procedural → semantic → episodic → working → rag → tool_results（与 SectionKey 枚举顺序一致）。输出 MUST 为一条合成 system 消息（系统提示 + 各区块结构化标签，空区块不渲染标签）加上 working 区块按原始 role 逐条输出的消息；条目标签为 `<memory type="…" source="…">`、`<summary source="…">`、`<knowledge source="…">`、`<tool_result source="…">`，外层包裹 `<procedural_memories>` / `<semantic_memories>` / `<session_summaries>` / `<knowledge_chunks>` / `<tool_results>`；procedural 包裹标签内首行 SHALL 注入"必须逐条遵守"的行为引导语。

#### Scenario: render-structure-and-empty-sections
- GIVEN procedural、semantic、episodic 各有一条候选，rag 与 tool_results 为空
- WHEN 装配并渲染
- THEN messages 为 system 一条加 working 原 role 消息
- AND system 正文含三条对应标签且不含 `<knowledge_chunks>` 与 `<tool_results>`

#### Scenario: procedural-wrapper-carries-directive
- GIVEN procedural 与 semantic 区块均有候选
- WHEN 渲染合成 system 消息
- THEN `<procedural_memories>` 内首行含"必须逐条遵守"
- AND semantic 区块之后不再出现该引导语

### Requirement: budget-profile-loading
预算 SHALL 从 `config/budgets/<profile>.yaml` 加载（目录解析顺序：`MEMORY_SERVICE_BUDGET_CONFIG_DIR` → CWD/config/budgets → 仓库根 config/budgets，三处均无即 BudgetConfigError），并 MUST 校验 version=1、reserve_output ∈ (0,1)、七区块齐全、占比 ∈ [0,1]、占比总和 ≤ 1.0、evict_order 非空且不含 system 与重复项。绝对预算 = round((window_tokens − reserve_output) × 占比)，system 恒为 -1（不裁剪但计入占用），available_tokens = window_tokens − reserve_output。

#### Scenario: three-profiles-converted-correctly
- GIVEN window_tokens 为 4000 且 profile 为 default
- WHEN 加载预算
- THEN available_tokens 为 3600，system 为 -1，semantic 为 540、working 为 1080、rag 为 900
- AND memory_first 的 semantic 大于 default 的 semantic，knowledge_first 的 rag 大于 default 的 rag

#### Scenario: invalid-profile-fails-fast
- GIVEN 请求一个不存在的 profile 名
- WHEN 加载预算
- THEN 抛出 BudgetConfigError
- AND 不回退到默认 profile

### Requirement: per-section-packing
区块内裁剪 SHALL 遵循：记忆类区块按 score 降序装入、单条超出剩余预算则跳过并继续尝试更小的条目；working 区块从最旧开始滑窗丢弃并 MUST 始终保留末条（当前问题）；system 区块永不裁剪。用量统计 SHALL 给出每个区块的 budget_tokens / included / dropped / tokens。

#### Scenario: score-eviction-keeps-high-score
- GIVEN semantic 区块有高、低分两条候选且预算只容一条
- WHEN 装配
- THEN 仅高分条目被纳入
- AND 该区块 included 为 1、dropped 为 1

#### Scenario: working-sliding-window-keeps-latest
- GIVEN working 窗口有三条消息且总 token 超预算
- WHEN 装配
- THEN 保留最新的两条（时间正序）
- AND messages 末条为当前问题原文

### Requirement: global-eviction-defense
当各区块 token 总和超过 available_tokens 时，系统 SHALL 按预算 profile 的 evict_order 从前往后整区块放弃；working 被放弃时只保留末条，且仅剩一条时跳过该区块；system MUST 不出现在 evict_order 中；无可放弃区块时保留现状不再循环。

#### Scenario: evict-order-clears-rag-before-procedural
- GIVEN default profile 且各区块候选使总量远超 available_tokens
- WHEN 装配
- THEN rag 区块先被清空、procedural 最后被清空
- AND working 仅剩末条且 total_tokens 不超过 available_tokens

#### Scenario: working-last-message-never-dropped
- GIVEN 全局超预算且 working 已只剩一条
- WHEN 继续淘汰
- THEN working 不再被选为牺牲区块
- AND 末条内容仍为当前问题

### Requirement: candidate-sourcing-and-dedup
build SHALL 按序取数：本会话 `rolling_summary` 直取注入 episodic（score=1.0，并跳过检索命中中 key 为 `session_summary:<sid>` 的条目防重复）；长期记忆检索结果按 memory_type 分桶注入 procedural / semantic / episodic；会话消息总数 ≤ `task_brief_max_session_messages`（默认 6）时把未完成任务简报注入 semantic（score 0.85，条数上限 `task_brief_limit` 默认 5）；RAG 命中注入 rag；本轮附件切片强制置顶注入 rag；working 窗口按 working 区块预算截取。返回的 citations SHALL 只含真正装入 rag 区块的命中。

#### Scenario: summary-direct-injection-and-same-key-dedup
- GIVEN 会话已有 rolling_summary 且检索也命中该摘要条目
- WHEN 装配
- THEN system 正文中该摘要仅出现一次
- AND 检索命中的 memory 条目按类型出现在对应区块标签内

#### Scenario: task-brief-only-in-early-session
- GIVEN 会话消息总数为 6 且存在未完成任务
- WHEN 装配
- THEN semantic 区块注入形如"未完成任务「标题」（状态/优先级）"的条目
- AND 消息总数超过 6 后不再注入该简报

### Requirement: source-degradation
各取数源 SHALL 独立降级、互不影响：embedding 未配置时 retriever 为 None（无记忆检索）；检索抛 EmbeddingError、RAG 检索异常、任务简报异常时均降级为空区块并记录告警；装配流程 MUST 不因任一取数失败而抛出。

#### Scenario: retrieval-unavailable-degrades-to-summary-and-window
- GIVEN 装配器的 retriever 为 None
- WHEN 执行 build
- THEN 仍返回 system 与 working 消息且 system 含 `<summary source="session:…">`
- AND semantic / procedural 区块为空且不抛异常

#### Scenario: rag-failure-degrades-to-empty-section
- GIVEN 知识检索器抛出异常
- WHEN 执行 build
- THEN rag 区块为空
- AND 其余区块按预算正常装配

### Requirement: context-preview-endpoint
`POST /api/context/preview` SHALL 只跑取数与装配（不调用生成模型、不落库），返回 profile、window_tokens、available_tokens、total_tokens、sections（顺序与 SectionKey 一致，含 budget_tokens / included / dropped / tokens）与 messages；入参 workspace_id / session_id / query / profile 由已登录成员提交。鉴权与校验 MUST 为：未带 token 401、会话不存在 404、session_id 非法 422、非 workspace 成员 403、预算配置错误 500（budget_config_error）。

#### Scenario: preview-roundtrip-returns-usage
- GIVEN 成员在 default profile 下请求预览
- WHEN 调用预览端点
- THEN 返回 profile 为 default、window_tokens 为 4000、available_tokens 为 3600
- AND sections 键顺序与 SectionKey 一致且 total_tokens 不超过 available_tokens

#### Scenario: preview-auth-and-validation
- GIVEN 未登录用户、不存在的会话、非法 session_id 与非成员用户
- WHEN 分别调用预览端点
- THEN 依次返回 401、404、422、403
- AND 非法 profile 返回 500 且错误码为 budget_config_error
