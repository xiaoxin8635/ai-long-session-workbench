## Purpose
定义 EchoDesk 前端工作台的已实现行为契约：七页面路由与登录守卫、POST-SSE 的字节流分帧解析、聊天流式状态机与工具确认续传、记忆与知识工作台、上下文装配预览与任务面板（brownfield 基线）。

## Requirements

### Requirement: spa-routing-and-page-capabilities
系统 SHALL 以 BrowserRouter 提供七页面路由：`/login` 公开，其余六页嵌于 RequireAuth 与 AppLayout 之下。

#### Scenario: unauthenticated-redirect-preserves-target
- GIVEN 会话中没有 access token
- WHEN 访问 `/tasks?status=doing`
- THEN 以 replace 模式重定向到 `/login?redirect=%2Ftasks%3Fstatus%3Ddoing`
- AND 登录成功后按 redirect 参数回跳原路径，缺省回跳 `/`

#### Scenario: seven-page-capabilities-under-guard
- GIVEN 已登录用户访问任意未匹配路径
- WHEN 路由命中 `*` 兜底规则
- THEN 重定向到 `/`
- AND 七个页面分别承载登录注册、三栏聊天、记忆工作台、知识库、任务、用量、workspace 成员设置

### Requirement: sse-post-stream-framing
系统 MUST 用 fetch + ReadableStream 自行分帧解析 SSE（聊天端点为 POST，EventSource 不支持），支持跨 chunk 撕裂、CRLF、多行 data、注释行与流尾冲刷。

#### Scenario: cross-chunk-tearing-and-tail-flush
- GIVEN 字节流被切成 `data: {"content":"你` 与 `好"}\n\ndata: [DONE]\n\n` 两片
- WHEN consumeSse 消费整条流
- THEN 依次产出 data 为 `{"content":"你好"}` 与 `[DONE]` 的两个事件
- AND 以 `data: tail` 无换行结束时仍冲刷出该事件

#### Scenario: crlf-multiline-and-comments-skipped
- GIVEN 事件流 `event: error\ndata: line1\ndata: line2\n\n`
- WHEN 解析该流
- THEN 事件名为 error 且 data 为 `line1\nline2`
- AND `\r` 与字段名后单个空格被剥离，注释行与连续空行不产生事件

### Requirement: chat-streaming-state-machine
系统 SHALL 以乐观追加驱动聊天流式：先落 user 与 assistant 两条本地占位消息，再随 onDelta 增量回填，finish 片落地 citations，失败时标记 failed 并展示错误条。

#### Scenario: optimistic-append-then-citations-landing
- GIVEN 草稿态会话与 workspace ws-1
- WHEN 发送「你好」且流回传 onSessionId("s-new")、两次 onDelta 与带 1 条 citation 的 finish
- THEN 消息列表为 user「你好」与 assistant「你好」且 citations 长度为 1
- AND activeSessionId 变为 s-new，streaming 复位为 false 且会话列表被刷新

#### Scenario: upstream-error-marks-failed
- GIVEN 流式过程中收到 onError("上游超时", "upstream_error") 或请求层抛出 Problem
- WHEN 该轮结束
- THEN 对应 assistant 消息 failed 为 true 且页面级 error 为后端 detail 文案

#### Scenario: duplicate-send-rejected-while-streaming
- GIVEN 第一轮流式尚未结束
- WHEN 再次调用 sendMessage
- THEN streamChat 仅被调用 1 次（用户主动中断保留已到达正文且不标记失败）

### Requirement: tool-confirm-card-and-resume
系统 SHALL 在助手消息挂起 external 工具调用时渲染确认卡片，并在有挂起确认时禁用输入框、发送与附件按钮；裁决经 POST /v1/chat/resume 续传。

#### Scenario: pending-confirmation-locks-input
- GIVEN 存在一条 resolved 未定义的 toolCall 的助手消息
- WHEN 渲染悬浮输入区
- THEN textarea 被禁用且占位文案为「等待工具确认…」
- AND 发送按钮与添加附件按钮同时禁用

#### Scenario: resume-approve-appends-and-resolves
- GIVEN 助手消息携带 call_id 为 c-9 的挂起调用
- WHEN 以 approve=true 裁决且续传流回传 onDelta「已抓取完成」
- THEN 该消息 content 变为「已抓取完成」且 resolved 为 approved

#### Scenario: resume-http-failure-unblocks-card
- GIVEN 同一挂起调用且 resume 请求在 HTTP 层失败（如 409 已终态）
- WHEN 异常被捕获
- THEN 仍写回 resolved 并展示错误提示，确认卡片不会永久卡住

### Requirement: memory-and-knowledge-workbench
系统 SHALL 提供记忆与知识工作台：记忆支持撤销式软删除、冲突裁决、版本链与事件流水，知识库支持解析状态轮询。

#### Scenario: undo-window-soft-delete
- GIVEN 列表中一条记忆条目
- WHEN 点击软删除
- THEN 条目立即从列表隐藏并弹出 5 秒撤销 Toast
- AND 撤销则不调用删除 API，窗口过后才真正发起删除

#### Scenario: conflict-adjudication-and-history
- GIVEN 状态为 conflicted 的记忆条目
- WHEN 在详情抽屉选择「保留本条」或「保留对手方」
- THEN 调用裁决接口完成冲突消解
- AND 抽屉可展开版本链与最近 20 条事件流水

#### Scenario: parsing-poll-until-embedded
- GIVEN 知识库文件列表中存在 parsing 状态的文件
- WHEN 页面停留于知识库页
- THEN 每 3 秒轮询一次文件列表直至状态变为 embedded 或 failed

### Requirement: context-preview-panel
系统 SHALL 提供上下文装配预览面板：POST /api/context/preview（workspace_id/session_id/query 在 body，独立 60s 超时），以预算条展示各区块 token 与裁剪明细。

#### Scenario: seven-section-token-bars
- GIVEN 已选中会话与模拟 query
- WHEN 点击装配预览
- THEN 展示 profile、window/available token 与各区块的 token/预算、纳入与裁剪条数
- AND 区块名映射 system/procedural/semantic/episodic/working/rag/tool_results

#### Scenario: unlimited-budget-shows-infinity
- GIVEN 某区块 budget_tokens 为 -1
- WHEN 渲染该区块预算条
- THEN 文案显示 `tokens / ∞` 且占比条为满格
- AND query 为空且会话中无用户消息时提示「请输入模拟 query（会话中尚无用户消息）」

### Requirement: tasks-board-transitions
系统 SHALL 按 TRANSITIONS 矩阵提供任务状态流转：open 可转 doing/done/abandoned，doing 可转 done/open/abandoned，done 与 abandoned 仅可转 open。

#### Scenario: transitions-matrix-rendered-per-status
- GIVEN 任务分别处于 open、doing、done、abandoned 状态
- WHEN 渲染任务卡片
- THEN 各自仅展示矩阵允许的流转按钮

#### Scenario: create-and-priority-toggle
- GIVEN 任务页已取到 workspace
- WHEN 提交标题与优先级创建，或点击星标切换优先级
- THEN 成功后回刷当前过滤视图（标题为空时创建被忽略）
