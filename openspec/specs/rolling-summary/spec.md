## Purpose

单会话上下文压缩：把滑出 Working Memory 窗口的消息段增量并入 `sessions.rolling_summary`，控制摘要体积，并沉淀为 episodic 记忆供跨会话检索。

## Requirements

### Requirement: trigger-on-window-slide
每轮回答落库后系统 SHALL 调用 `RollingSummarizer.maybe_compress`；滑出段 MUST 为 Working Memory 全量条目与 `window(max_tokens=context_window_tokens，默认 4000)` 返回窗口的差集；滑出段为空时返回 False 且不调用语言模型；会话不存在或状态非 active 时返回 False。

#### Scenario: sliding-segment-triggers-compression
- GIVEN 窗口内累计 4 条消息且总 token 超过窗口预算（仅 2 条可留在窗口内）
- WHEN 触发一次压缩
- THEN 返回 True
- AND 语言模型被调用且入参包含滑出段原文

#### Scenario: cache-hit-skips-llm
- GIVEN 窗口内只有一条远小于预算的消息（无滑出段）
- WHEN 触发一次压缩
- THEN 返回 False
- AND 语言模型零调用且 `rolling_summary` 保持为空

### Requirement: incremental-merge
系统 SHALL 以"旧摘要 + 滑出段原文"构造增量合并请求，输出一份新的完整摘要；首次压缩时旧摘要占位为"（暂无）"。合并 prompt MUST 约束：只记录用户陈述的事实/偏好/决定/约定/任务，助手的建议、方案与话术不得写成"用户……"；冲突时以新增对话段的最新说法为准；条目式中文、不复述寒暄。

#### Scenario: first-compression-uses-empty-placeholder
- GIVEN 会话尚无 rolling_summary
- WHEN 首次压缩
- THEN 合并请求的系统提示含"（暂无）"
- AND 用户消息含滑出段的 role 与 content 原文

#### Scenario: prompt-separates-user-facts-from-assistant-output
- GIVEN 增量合并 prompt 模板
- WHEN 检查其系统提示
- THEN 含"只记录**用户**陈述"的限制语
- AND 含禁止把助手建议写成"用户……"的约束

### Requirement: second-level-compression
增量合并后的摘要 token 超过 `rolling_summary_max_tokens`（默认 800）时，系统 SHALL 再调用一次压缩 prompt（要求保留关键事实、长度约为原文一半以内）；若仍超预算，SHALL 记录 `rolling_summary_still_over_budget` 告警并保留该摘要，不视为失败。

#### Scenario: over-budget-summary-compressed-once
- GIVEN 增量合并返回的摘要 token 超过 800
- WHEN 完成本轮压缩
- THEN 语言模型被调用两次且第二次输入为该超限摘要原文
- AND 落库内容为第二次压缩后的摘要

#### Scenario: still-over-budget-kept-with-warning
- GIVEN 二级压缩后的摘要仍超过 800 token
- WHEN 完成本轮压缩
- THEN 该摘要仍被落库
- AND 日志记录 `rolling_summary_still_over_budget` 且不对外抛错

### Requirement: persistence-and-episodic-archive
压缩成功 SHALL 在同一事务内完成：写入 `sessions.rolling_summary`、upsert 该会话的 episodic 记忆（key 为 `session_summary:<session_id>`，memory_type=episodic，confidence 0.9、importance 0.7，source_session_id 指向本会话）、把 Working Memory 窗口 LTRIM 到窗口长度，随后提交。upsert MUST 幂等：key 已存在时更新内容、状态回 active、`version` 自增且不新建条目。

#### Scenario: first-compression-creates-episodic-entry
- GIVEN 一次成功的压缩
- WHEN 查询该 workspace 的记忆
- THEN 存在唯一一条 episodic 记忆，key 为 `session_summary:<sid>`、status 为 active、version 为 1
- AND 窗口只剩预算内的消息（与摘要不重不漏）

#### Scenario: repeated-compression-bumps-version
- GIVEN 同一会话再次产生滑出段并压缩
- WHEN 查询该会话的摘要记忆
- THEN 条目仍唯一且 version 为 2、内容为最新摘要
- AND 事件流水依次为 CREATED、UPDATED

### Requirement: session-lock-and-degradation
`maybe_compress` SHALL 全程持有会话级分布式锁（与消息追加互斥）；锁竞争时放弃本轮并返回 False（下一轮再压）；其余异常记录日志后返回 False。任何情况下 MUST 不影响主对话流程。

#### Scenario: lock-contention-skips-round
- GIVEN 另有请求正持有该会话锁追加消息
- WHEN 触发压缩
- THEN 返回 False
- AND `rolling_summary` 不被修改且不抛异常

#### Scenario: summarizer-error-degrades-silently
- GIVEN 摘要语言模型抛出异常
- WHEN 触发压缩
- THEN 返回 False 且异常不外抛
- AND 会话状态与窗口保持原样

### Requirement: bounded-context-over-long-dialog
长对话中每轮装配的上下文 SHALL 保持有界：反复压缩下每轮 token 不超过"系统提示 + 摘要上限 + working 窗口 + 标签包装余量"；摘要 MUST 以 `<summary source="session:<sid>">` 注入合成 system 消息，working 窗口末条按原始 role 保留。

#### Scenario: hundred-rounds-stay-bounded
- GIVEN 连续 100 轮对话且每轮触发压缩并装配上下文
- WHEN 逐轮统计 messages 总 token
- THEN 每轮均不超过既定上界（不随轮次线性增长）
- AND 摘要记忆仍唯一且 version 至少为 2

#### Scenario: summary-injected-into-system-message
- GIVEN 会话已生成滚动摘要
- WHEN 装配上下文
- THEN messages 首条 role 为 system 且含 `<summary source="session:<sid>">`
- AND messages 末条为窗口最新消息的原文
