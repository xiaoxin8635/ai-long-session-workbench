## Purpose
定义 EchoDesk 用量与观测子系统的已实现行为契约：token_usages 按轮次落库与区块拆分、用量聚合端点、Langfuse trace 埋点与 no-op 降级、成本折算与用量看板渲染（brownfield 基线）。

## Requirements

### Requirement: token-usage-per-turn
系统 SHALL 在每轮 finalize 时落一条 token_usages：turn_no 为会话内最大值 +1（首轮为 1），prompt/completion 优先取上游 usage，缺失时按装配结果本地估算。

#### Scenario: upstream-usage-preferred
- GIVEN 上游 LLM 回传 usage 10/8 的假实现
- WHEN 在同一会话连续完成两轮非流式对话
- THEN 库中存在 turn_no 依次为 1、2 的两条记录且 prompt/completion 均为 10/8
- AND 上游 usage 优先于本地估算，两条记录 cost_usd 为 0

#### Scenario: stream-local-estimation
- GIVEN 流式回传零值 usage
- WHEN 该轮流式结束
- THEN prompt_tokens 不小于「你是 EchoDesk」的 token 数
- AND completion_tokens 等于回答文本的 token 数

#### Scenario: sections-follow-assembled-context
- GIVEN 会话已注入滚动摘要（episodic 区块有量）且无 RAG 与工具注入
- WHEN 落一轮用量
- THEN memory_tokens 大于 0 且 rag_tokens、tool_tokens 为 0
- AND 装配为 None 时三个区块均记 0

### Requirement: usage-summary-api
系统 SHALL 提供 `GET /api/usage/summary`：days 取值 1~90（缺省 7），按 workspace_id 聚合窗口内总量、会话数、轮次数与按日序列。

#### Scenario: window-filter-and-totals
- GIVEN 窗口内两轮 10/8 与一条 30 天前的旧记录
- WHEN 以 `days=7` 请求汇总
- THEN turns 为 2、sessions 为 1、total_tokens 为 36
- AND by_day 长度为 1 且日期为当日 UTC 日期

#### Scenario: auth-and-range-guards
- GIVEN 已登录用户与其他用户各自的 workspace
- WHEN 无令牌访问、跨 workspace 访问、以 `days=0` 访问
- THEN 依次返回 HTTP 401、403、422

### Requirement: langfuse-turn-trace
系统 SHALL 以 `chat.turn` 作为对话轮次的根 span：经 propagate_attributes 提升 session_id/user_id/tags，trace_id 归一为 32 位无连字符 hex。

#### Scenario: root-span-wraps-stream-and-resume
- GIVEN 流式或非流式对话以及工具裁决续传请求
- WHEN 处理该轮
- THEN 全程包在 tracing.turn 内，metadata 含 workspace_id、stream、model 与截断后的 query
- AND trace_name 固定为 chat.turn

#### Scenario: trace-id-hex-normalization
- GIVEN 带连字符的标准 UUID 或非法字符串
- WHEN 归一 trace id
- THEN 输出 32 位无连字符 hex
- AND 来源缺失或非法时新生成合法 hex，不抛异常

### Requirement: langfuse-noop-degradation
系统 MUST 在三项 Langfuse 配置任一为空时降级为 no-op：客户端为 None，各 span 入口返回空观察对象且不阻断业务。

#### Scenario: unconfigured-everything-noop
- GIVEN 未配置 Langfuse 的测试/本地环境
- WHEN 调用 turn、span、generation 与 shutdown_flush
- THEN get_client 返回 None 且四个入口均不抛错
- AND 空观察对象的 update 返回自身，支持链式调用

#### Scenario: snippet-truncation
- GIVEN 需要上报的对话正文
- WHEN 生成上报片段
- THEN None 返回空串、短文原样、超长文本截断并标注原始长度
- AND 默认上限为 500 字符

### Requirement: pricing-and-cost-usd
系统 SHALL 按 config/pricing.yaml 的价目表折算成本：模型未收录或价目缺失时记 0，绝不阻断用量落库。

#### Scenario: known-model-cost
- GIVEN 价目表中收录 qwen-plus（1K in 0.000111 / 1K out 0.000278）
- WHEN 折算 1000 输入与 1000 输出
- THEN 成本为 0.000389（USD，6 位小数）
- AND record_turn 落库时按 Settings.llm_model 折算 cost_usd

#### Scenario: unknown-model-zero
- GIVEN 模型为 None 或未收录
- WHEN 折算任意 token 量
- THEN 成本为 0
- AND 价目文件缺失或非法时仅告警并返回空表

### Requirement: usage-dashboard
系统 SHALL 以纯 CSS 渲染用量看板：窗口 7/14/30/90 切换、五张概览卡、三区块占比条与按日趋势柱图，不引入图表库。

#### Scenario: overview-and-section-ratios
- GIVEN 汇总为 prompt 10000 / completion 3000 / memory 2000 / rag 800 / tool 120、sessions 4、turns 26
- WHEN 渲染看板
- THEN 显示总 token「13,000」、会话数「4」、轮次数「26」
- AND 区块占比按 prompt 计算，记忆显示「2,000 · 20.0%」、工具显示「120 · 1.2%」

#### Scenario: empty-window-placeholder
- GIVEN 窗口内 by_day 为空数组
- WHEN 渲染按日趋势
- THEN 展示「窗口内暂无用量数据」
- AND 请求失败时展示可关闭的错误条
