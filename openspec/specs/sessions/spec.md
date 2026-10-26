## Purpose
定义 EchoDesk 会话域的现有行为契约：多会话的创建、列表分页、重命名与归档状态机、软删除，消息的 append-only 追加与 token/metadata 落库，Redis 会话锁加乐观锁的并发写语义，以及长会话回放分页。本文件为已实现行为的 brownfield 基线。

## Requirements

### Requirement: session-crud-and-status-machine
系统 SHALL 提供会话 CRUD：创建返回 201（title 缺省为「新会话」，status 为 `active`，version 为 1，token_total 为 0），PATCH 支持重命名与归档/恢复，DELETE 返回 204 并软删除（`status` 转 `deleted`）。

#### Scenario: rename-archive-restore-cycle
- GIVEN 已创建的会话（title「新会话」，status active）
- WHEN PATCH 提交 `{"title":"需求评审"}` 后再提交 `{"status":"archived"}`
- THEN 两次均返回 HTTP 200，title 与 status 依次为「需求评审」、`archived`
- AND 再次提交 `{"status":"active"}` 后会话回到 `active`

#### Scenario: patch-deleted-status-rejected
- GIVEN 已创建的会话
- WHEN PATCH 提交 `{"status":"deleted"}`
- THEN 返回 HTTP 422，错误码 `invalid_operation`
- AND 仅 DELETE 端点可将软删除后的会话置为 `deleted`，删除后 GET 详情返回 HTTP 404

### Requirement: session-list-pagination-and-search
系统 SHALL 以 `Page{items,total}` 返回会话列表：limit 取值 1~100（默认 20）、offset ≥ 0（默认 0），按 `updated_at` 倒序，支持 `q` 标题关键词模糊过滤，始终排除软删除会话。

#### Scenario: pagination-boundaries-and-total
- GIVEN 同一 workspace 下已创建 5 个会话
- WHEN 以 `limit=2&offset=0` 请求列表
- THEN `total` 为 5 且 `items` 长度为 2
- AND 以 `offset=4` 请求时 `items` 长度 1，以 `limit=0` 或 `limit=101` 请求返回 HTTP 422

#### Scenario: archived-hidden-until-requested
- GIVEN 已归档的唯一会话
- WHEN 不带参数请求列表
- THEN `total` 为 0
- AND 带 `include_archived=true` 请求时 `total` 为 1；`q=不存在` 时 `total` 为 0

### Requirement: message-append-token-and-metadata
系统 SHALL 在追加消息时按内容计算 `token_count` 并落库 `metadata`（缺省 `{}`），返回 201 的 MessageRead；消息 append-only，无更新与删除端点。

#### Scenario: append-persists-count-and-metadata
- GIVEN 处于 active 的会话
- WHEN POST `/messages` 提交 `{"role":"user","content":"你好，世界","expected_version":1}`
- THEN 返回 HTTP 201，`token_count` 大于 0 且 `metadata` 为 `{}`
- AND 会话 `version` 前进为 2、`last_message_at` 非空、`token_total` 等于所有消息 `token_count` 之和

#### Scenario: invalid-role-or-empty-content-rejected
- GIVEN 处于 active 的会话
- WHEN 提交 role 不在 `user|assistant|tool|system` 之内，或 content 为空串
- THEN 返回 HTTP 422

### Requirement: redis-lock-and-optimistic-version
系统 MUST 在同一会话的写路径上先获取 Redis 分布式锁（`lock:session:{id}`，SET NX PX，TTL 5 秒，获取超时 3 秒），再以 `expected_version` 与 `version` 条件更新做乐观锁，锁内提交；任一冲突返回 409。

#### Scenario: stale-expected-version-conflicts
- GIVEN 会话当前 version 为 2
- WHEN 客户端仍以 `expected_version=1` 追加消息
- THEN 返回 HTTP 409，错误码 `conflict`
- AND 归档状态的会话追加消息同样返回 HTTP 409

#### Scenario: ten-concurrent-appends-no-loss
- GIVEN 同一会话并发发起 10 次不带 `expected_version` 的追加
- WHEN 全部请求执行完毕
- THEN 10 次均返回 HTTP 201
- AND 消息总数为 10、会话 `version` 为 11、`token_total` 等于各消息 `token_count` 之和

### Requirement: long-session-replay-pagination
系统 SHALL 为长会话提供时间正序的消息回放分页：limit 取值 1~100（默认 50）、offset ≥ 0，返回该会话消息总数；会话详情附带首页消息（limit 20）供首屏渲染。

#### Scenario: messages-returned-in-chronological-order
- GIVEN 会话中先后追加「你好，世界」与「hello world from test」
- WHEN GET `/messages` 不带分页参数
- THEN `total` 为 2 且 items 按 `created_at` 升序排列，首条内容前缀为「你好，世界」

#### Scenario: detail-embeds-first-page
- GIVEN 会话中已有多条消息
- WHEN GET 会话详情端点
- THEN 响应含 `messages` 字段且最多 20 条
- AND 超出首页的消息需通过 `/messages` 分页获取

### Requirement: workspace-bound-session-endpoints
所有会话端点 MUST 通过 `workspace_id` query 参数绑定 workspace 并由 `CurrentWorkspaceQuery` 做成员校验，资源查询本身强制 `workspace_id` 过滤。

#### Scenario: cross-workspace-resource-not-found
- GIVEN 用户 B 仅拥有自己的 workspace，且存在属于 A 的会话
- WHEN B 携带自己的 `workspace_id` 访问该会话的详情、消息追加与删除
- THEN 三个请求均返回 HTTP 404

#### Scenario: foreign-workspace-forbidden
- GIVEN 用户 B 不是 A 的 workspace 成员
- WHEN B 携带 A 的 `workspace_id` 请求会话列表
- THEN 返回 HTTP 403，错误码 `permission_denied`
- AND B 的列表中不含 A 的会话数据

### Requirement: cascade-memory-archival-on-delete
系统 SHALL 在 DELETE 端点支持 `cascade_memories` 开关：为 true 时把来源为该会话的记忆转为 `archived`，再软删除会话并返回 204；为 false 时不触碰记忆。

#### Scenario: cascade-archives-source-memories
- GIVEN 会话存在一条 `source_session_id` 指向它的 ACTIVE 记忆
- WHEN 以 `cascade_memories=true` 删除该会话
- THEN 返回 HTTP 204
- AND 该记忆状态变为 `archived`

#### Scenario: default-delete-keeps-memories
- GIVEN 同一场景未传 `cascade_memories`
- WHEN 删除该会话
- THEN 返回 HTTP 204 且来源记忆状态保持 ACTIVE
