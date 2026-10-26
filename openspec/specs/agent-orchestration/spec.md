## Purpose

对话主链路经 LangGraph 图编排（load_context → generate → tools → postprocess，工具循环回流 generate），以会话级 checkpoint 支撑 external 工具中断后的确认与恢复，并把过程事件回推为 SSE 分片。

## Requirements

### Requirement: graph-topology-and-state
编译图 SHALL 固定为 START → load_context → generate，generate 后按 `route_after_generate` 条件分支：`pending_calls` 非空走 tools 再回到 generate，否则走 postprocess → END。仅 GraphState 基础字段（messages、answer、tool_calls、pending_calls、iterations、用量计数等）进 checkpoint；db、user、llm、tools 与回调等运行依赖 MUST 经 `configurable.ctx` 注入，缺失时节点 MUST 抛 RuntimeError。load_context SHALL 复用 ContextBuilder 装配 messages，postprocess SHALL 复用 ChatService.finalize 落库与用量。

#### Scenario: tool-loop-roundtrip-then-converge
- GIVEN 脚本化 LLM 首轮返回 todo.create 调用、次轮返回正文
- WHEN 以开启工具的参数 ainvoke 整图
- THEN 最终 answer 为次轮正文且 iterations 为 2
- AND tool_calls 含一条 status 为 success 的记录

#### Scenario: missing-context-fails-fast
- GIVEN 图的 config 未注入 configurable.ctx
- WHEN 调用任一图节点
- THEN 抛出 RuntimeError 提示缺少 configurable.ctx
- AND 不产生任何数据库副作用

### Requirement: tool-iteration-guard
generate SHALL 每次执行把 `iterations` 自增 1；当 `iterations` 已达 MAX_TOOL_ITERATIONS（8）或 `ctx.tools` 为空（`enable_tools` 为 false）时，本轮 MUST 改用不带 tools 参数的普通流式生成强制收敛为文本回答。

#### Scenario: ninth-round-forced-convergence
- GIVEN LLM 连续 8 轮都返回工具调用
- WHEN 图执行到第 9 轮 generate
- THEN 该轮走无工具协议的流式收敛并返回文本
- AND iterations 为 9 且 tool_calls 恰为 8 条

#### Scenario: tools-disabled-pure-chat
- GIVEN 请求 metadata 的 enable_tools 为 false
- WHEN 执行一轮对话
- THEN 全程未调用带工具的流式接口
- AND tool_calls 为空列表且 answer 为 LLM 文本

### Requirement: sse-event-contract
流式响应 SHALL 以 OpenAI chunk 形式回推：正文增量走 `delta`（首片附 `metadata.session_id`）；external 待确认走 `tool_call` 事件并通过 metadata.tool_call 下发 `{call_id, tool, args, risk}`；收尾 MUST 发出 finish_reason 为 stop 的 finish 片（metadata 含 session_id，存在挂起时附 `pending_confirmation`，有引用时附 `citations`，并附 usage.completion_tokens）并以 `data: [DONE]` 结束；图内异常 MUST 转为 `{"error": {"message": ..., "type": ...}}` 后再发 `data: [DONE]`结束流。

#### Scenario: delta-first-chunk-then-finish
- GIVEN 一次无工具调用的流式对话
- WHEN 消费 SSE 流
- THEN 首片的 metadata 含 session_id，其后为若干 content 增量片
- AND 末片为 finish_reason 为 stop 的 chunk 并紧跟 `data: [DONE]`

#### Scenario: error-event-terminates-stream
- GIVEN 上游 LLM 抛出 LLMError
- WHEN 消费 SSE 流
- THEN 收到 type 为 upstream_error 的 error 事件
- AND 随后收到 `data: [DONE]` 且不再有 delta 片

### Requirement: checkpoint-persistence-and-degradation
生产 SHALL 用 AsyncPostgresSaver 持久化 checkpoint（psycopg 池 min_size 1、max_size 4、连接 autocommit 且 search_path 指向专用 schema `agent_ckpt`，启动时幂等建 schema 与表），thread_id MUST 为 `<workspace_id>:<session_id>`；PG 不可达或 setup 失败时 MUST 回落 InMemorySaver（仅记 warning），对话链路仍可用而跨进程恢复能力降级。

#### Scenario: session-scoped-thread-id
- GIVEN 某 workspace 与会话正在执行对话
- WHEN 构造 LangGraph 运行配置
- THEN thread_id 为 workspace 与 session 双 ID 拼接串
- AND 不同会话的 checkpoint 互不覆盖

#### Scenario: postgres-unavailable-falls-back
- GIVEN 数据库连接在启动时失败
- WHEN AgentRuntime.start 执行
- THEN checkpointer 为 InMemorySaver 并记录降级日志
- AND 服务仍可正常编译图并处理对话

### Requirement: external-interrupt-and-resume
generate 发现 external 工具调用时 SHALL 在 checkpoint 边界前完成副作用：落 `pending` 审计、提交、经 on_event 推送 tool_call 事件；tool_node 对 external 条目 MUST 调用 `interrupt({call_id, tool, args, risk})` 挂起图，ainvoke 返回带 `__interrupt__` 且 answer 为空的结果。POST /v1/chat/resume SHALL 预检 workspace 成员（403）、会话存在（404）、审计行存在（404）且状态仍为 pending（409），再以 `Command(resume={call_id, approve})` 恢复：resume 值非 dict、call_id 不匹配或 approve 非 true 一律按拒绝处理（落 denied 且 handler 不执行）；审计已非 pending 时 MUST 拒绝重复执行。

#### Scenario: approve-resume-executes-and-converges
- GIVEN 图因 external 工具调用已挂起且存在 pending 审计
- WHEN 以 approve=true 恢复同一 thread
- THEN handler 执行一次且审计推进为 success 并记 approver_id
- AND 后续轮次收敛产出 answer 并落库

#### Scenario: deny-resume-rejects-without-execution
- GIVEN 图同样处于 external 挂起态
- WHEN 以 approve=false 恢复
- THEN 审计为 denied 且 result_digest 为「用户拒绝执行」
- AND 拒绝说明被回灌给 LLM 供其向用户解释

#### Scenario: non-pending-resume-precheck
- GIVEN 该调用审计已处于 denied 终态
- WHEN 调用 POST /v1/chat/resume
- THEN 响应 409 且错误码为 `tool_call_not_pending`
- AND checkpoint 状态不被改动

### Requirement: tool-result-feedback-and-degradation
tool_node 结果 MUST 以 role=tool 消息回灌（tool_call_id 取自 LLM 调用 ID，超过 4000 字符者截断并追加「…[已截断]」），追加 tool_calls 摘要记录并清空 pending_calls；read_only/write 复用 execute_tool，未注册工具降级为「工具 X 未注册」失败文本，参数校验 AppError 降级为带 detail 的失败文本，两者 MUST 不炸图且未注册调用不产生审计行。

#### Scenario: result-fed-back-to-next-round
- GIVEN write 工具在首轮成功执行
- WHEN 进入第二轮 generate
- THEN LLM 输入含一条 role=tool 消息且 tool_call_id 与调用 ID 对齐
- AND 其正文含工具返回结果的关键内容

#### Scenario: unregistered-tool-degrades-in-graph
- GIVEN LLM 请求调用未注册的工具
- WHEN tool_node 处理该条目
- THEN 回灌消息含「未注册」且记录 status 为 failed
- AND 图仍收敛产出 answer 且无审计行生成

### Requirement: error-mapping-and-usage-accumulation
图中异常 MUST 转 SSE error 事件并携带原始异常类名；非流式路径 MUST 按 kind 映射：LLMError 转 502（`llm_upstream_error`），其余转 500（`agent_internal_error`）；LLM 未配置 MUST 转 503（`llm_not_configured`）。多轮调用 SHALL 累加 prompt_tokens 与 completion_tokens，并在 postprocess 按累加值记账；resume 路径 assembled 为空时用量区块明细记 0。

#### Scenario: upstream-failure-maps-to-502
- GIVEN 上游 LLM 流式调用失败
- WHEN 以非流式方式请求对话
- THEN 响应 502 且错误码为 `llm_upstream_error`
- AND 错误类别未与内部异常混淆

#### Scenario: multi-round-usage-accumulation
- GIVEN 两轮 LLM 调用脚本用量分别为 5/4 与 3/2
- WHEN 走完工具循环并落库用量
- THEN 记账的 prompt_tokens 为 8 且 completion_tokens 为 6
- AND 用量行关联到当前会话
