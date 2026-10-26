## Purpose

工具子系统以进程级注册表统一登记内置工具与 MCP 外部工具（名称/参数 schema/风险分级/handler），并按 read_only、write、external 三级决定直行或确认流，全部调用落 tool_call_logs 审计；MCP server 支持运行期热插拔，单 server 故障只部分降级。

## Requirements

### Requirement: tool-registry-and-schema-export
工具 MUST 以 ToolDefinition 登记到进程级 `default_registry`（name、description、risk、args_model、统一 handler 签名 `(db, *, ws_id, user, session_id, args)`）；同名 register SHALL 覆盖。内置工具 SHALL 包含 todo.create/todo.list/todo.update/todo.complete、doc.read、web.fetch。`openai_tools_schema()` MUST 按名排序导出 OpenAI functions 数组，parameters 取 args_model 的 JSON Schema，无参工具为 `{"type":"object","properties":{}}`。GET /api/tools SHALL 返回全量清单（含 args_schema 与 risk）。

#### Scenario: builtin-tools-with-expected-risk
- GIVEN 服务已启动且 builtin 包已导入
- WHEN 调用 GET /api/tools
- THEN todo.create 的 risk 为 write、doc.read 为 read_only、web.fetch 为 external
- AND todo.create 的 args_schema.properties 含 title

#### Scenario: schema-export-for-tool-protocol
- GIVEN 注册表含一个有参工具与一个无参工具
- WHEN 生成 OpenAI tools 参数
- THEN 有参项的 parameters 来自其 args_model 的 JSON Schema
- AND 无参项的 parameters 为 `{"type":"object","properties":{}}`

### Requirement: risk-level-execution-policy
POST /api/tools/execute SHALL 按 risk 分级：read_only 与 write 直接执行并同步返回终态（HTTP 200，body 含 status/result）；external 先落一条 `pending` 审计并提交，再返回 HTTP 202 且 `requires_confirmation` 为 true、`call_id` 为该审计行 ID。工具未注册 MUST 返回 404（`tool_not_found`），参数不满足 args_model 或无参工具被传参 MUST 返回 422（`tool_args_invalid`）。

#### Scenario: read-only-tool-executes-immediately
- GIVEN 知识库内已有 manual.md
- WHEN 执行 doc.read 且参数为合法文件名
- THEN 响应 200 且 status 为 success
- AND result.found 为 true 且正文中含文件内容

#### Scenario: external-tool-returns-202-pending
- GIVEN 一个 risk 为 external 的已注册工具
- WHEN 执行它并传合法参数
- THEN 响应状态码为 202 且 body.status 为 pending
- AND body.requires_confirmation 为 true 且 call_id 非空

### Requirement: external-confirmation-flow
external 调用 SHALL 由后台任务轮询 Redis 键 `tool:confirm:<call_id>`（间隔 1 秒）等待裁决，窗口为 `MEMORY_SERVICE_TOOL_CONFIRM_TIMEOUT_SECONDS`（默认 60 秒）；POST /api/tools/calls/{id}/confirm MUST 写入 `approve`/`deny`（键 TTL 为超时秒数 + 30 秒）并记录 `approver_id`。approve 后执行 handler 并落 success 或 failed；deny 落 denied（`result_digest` 为「用户拒绝执行」）；窗口内无裁决落 timeout（「等待用户确认超时，已拒绝执行」）。已处于非 pending 终态的调用再次确认 MUST 返回 409（`tool_call_not_pending`），不存在的记录返回 404，非本 workspace 的调用不可见。

#### Scenario: approve-executes-and-audits-approver
- GIVEN 一条 external 调用的 pending 记录
- WHEN 用户在窗口内 confirm approve=true
- THEN 审计终态为 success 且 result_digest 含 handler 返回值
- AND approver_id 为确认人用户 ID

#### Scenario: no-decision-times-out-denied
- GIVEN 确认等待窗口被配置为 1 秒且用户未裁决
- WHEN 等待窗口耗尽
- THEN 审计终态为 timeout 且 result_digest 含「超时」
- AND handler 从未被执行

### Requirement: tool-call-audit
每次调用 SHALL 落 tool_call_logs（workspace_id、session_id、tool_name、risk_level、args、status、result_digest、approver_id、created_at），external 的 pending 行 MUST 在派发等待任务前提交。结果 MUST 摘要化：JSON 序列化超过 `MEMORY_SERVICE_TOOL_RESULT_MAX_CHARS`（默认 2000）时截断并追加「…[已截断，原始 N 字符]」。GET /api/tools/calls SHALL 按 created_at 倒序返回，可按 session_id 过滤，limit 取 1~100（默认 20）。

#### Scenario: write-call-audit-with-session-link
- GIVEN 执行 todo.create 并携带 session_id
- WHEN 查询 GET /api/tools/calls
- THEN 首条记录 tool_name 为 todo.create 且 session_id 一致
- AND status 为 success 且 result_digest 含任务标题

#### Scenario: long-result-truncated-in-digest
- GIVEN handler 返回序列化后超过 2000 字符的结果
- WHEN 调用落审计
- THEN result_digest 长度不超过截断上限加后缀长度
- AND 后缀含「已截断」与原始字符数

### Requirement: execution-failure-degradation
handler 抛异常时 SHALL 降级为 `failed` 终态并把错误文本写入 result_digest（`error: <异常>`，同受 2000 字符截断），HTTP 仍为 200 且 body.error 为异常文本；执行 MUST 包裹 `tool.invoke` 观测 span（mode 为 immediate/confirmed/chat_confirmed）。工具级失败 MUST 不触发 5xx，也不向调用方抛异常。

#### Scenario: handler-error-returns-failed-text
- GIVEN 传入不存在的 task_id 执行 todo.update
- WHEN 调用 /api/tools/execute
- THEN 响应码为 200 且 status 为 failed
- AND 审计行 status 为 failed 且 error 文本可读

#### Scenario: invalid-args-rejected
- GIVEN 执行 todo.create 且 title 为空串
- WHEN 请求到达执行器
- THEN 响应码为 422 且错误码为 `tool_args_invalid`
- AND 未产生任何审计行

### Requirement: mcp-transport-auth-and-connection
MCP server 配置 SHALL 由 `MEMORY_SERVICE_MCP_SERVERS_JSON`（JSON 数组）注入：`name` 需匹配 `^[a-z0-9][a-z0-9_-]{0,63}$`，`transport` 取 http/sse/stdio，http 与 sse MUST 带 url、stdio MUST 带 command；坏 JSON、非数组与非法项 MUST 逐条跳过而非抛错。远端 http 经 `create_mcp_http_client(headers)` 注入自定义头、sse 直传 headers 以承载 API key 鉴权。单 server 连接超时取 `MEMORY_SERVICE_MCP_CONNECT_TIMEOUT_SECONDS`（默认 10 秒），连接失败或超时 SHALL 回收后台任务并返回空工具清单；MCP 工具默认 risk 取 `MEMORY_SERVICE_MCP_TOOL_RISK`（默认 external，非法值降级 external）。

#### Scenario: bad-config-items-skipped
- GIVEN 配置串含缺 url 项、缺 command 项与含大写字母名称项，另有一条合法 stdio 与一条合法 http
- WHEN 解析配置
- THEN 仅保留两条合法配置
- AND 过程不抛出异常

#### Scenario: headers-injected-for-auth
- GIVEN transport 为 sse 且 headers 为 `{"Authorization": "Bearer sk-test"}`
- WHEN 建立连接（对端立即失败）
- THEN sse_client 收到的 headers 与配置一致
- AND start() 返回空清单并进入降级

### Requirement: mcp-hot-plug-management
连接成功的 server SHALL 以 `mcp.<name>.<tool>` 注册进 `default_registry`（描述缺省回落到工具名与 server 名，参数模型由远端 input_schema 生成），远端 is_error 结果 MUST 抛 McpToolError。GET /api/mcp/servers SHALL 返回配置加运行态（connected、tools 列表，headers 值打码为 `***`）；POST MUST 先试连再落库（201，source 为 user），重名 409、配置不合法 422、连接失败 502 且不落库；DELETE 断连并注销 `mcp.<name>.*` 后返回 204，配置不存在 404、`env` 来源行 400。启动 bootstrap SHALL 同步 env 种子行并逐个连接启用行，任一 server 连接失败仅告警跳过。

#### Scenario: hot-add-registers-tools-immediately
- GIVEN 一个可连通的 MCP server 尚未接入
- WHEN POST /api/mcp/servers 新增该 server
- THEN 响应 201 且 GET /api/tools 立刻出现 `mcp.<name>.*` 工具
- AND GET /api/mcp/servers 中该行 connected 为 true

#### Scenario: connect-failure-does-not-persist
- GIVEN 新 server 指向无监听端口
- WHEN POST /api/mcp/servers
- THEN 响应 502 且错误码为 `mcp_connect_failed`
- AND mcp_servers 表未新增该行且连接池无残留

#### Scenario: partial-startup-degradation
- GIVEN 配置了两个 server 而其一不可达
- WHEN 服务启动执行 bootstrap
- THEN 可达 server 的工具正常注册
- AND 启动流程不被不可达者阻塞
