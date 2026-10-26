## Purpose
定义 EchoDesk 多租户（workspace）隔离的现有行为契约：三道防线——repository 强制 workspace_id 过滤、服务层成员与角色鉴权、向量库按 workspace 分区——以及 owner/admin/member 角色模型与跨 workspace 访问的拒绝行为。本文件为已实现行为的 brownfield 基线。

## Requirements

### Requirement: workspace-scoped-repository-filter
系统 MUST 让所有业务仓储（session、message、memory、knowledge、task、tool_call、usage）的查询强制携带 `workspace_id` 条件，使跨 workspace 的资源在仓储层即视同不存在。

#### Scenario: foreign-session-filtered-out
- GIVEN 属于 workspace A 的会话
- WHEN 以 workspace B 的 id 查询该会话（按主键）
- THEN 查询结果为 None
- AND 上层据此返回 HTTP 404，不暴露该会话的存在

#### Scenario: list-queries-scoped-by-workspace
- GIVEN 两个 workspace 各自拥有会话
- WHEN 以 A 的 `workspace_id` 分页查询会话列表
- THEN `total` 与 `items` 仅包含 A 的会话
- AND 同一查询在 memory / knowledge / task / usage 仓储上同样带 `workspace_id` 过滤

### Requirement: membership-and-workspace-id-validation
系统 SHALL 在服务层（`get_workspace`、`get_workspace_from_query`、`require_ws_member`）校验当前用户是否为目标 workspace 成员；非成员与 workspace 不存在统一返回 403（错误码 `permission_denied`），body 中 `workspace_id` 非 UUID 时返回 422（错误码 `invalid_metadata`）。

#### Scenario: non-member-access-forbidden
- GIVEN 用户 A 与用户 B 各自的个人 workspace
- WHEN A 携带 B 的令牌访问 B workspace 的详情、成员列表与添加成员接口
- THEN 三个请求均返回 HTTP 403

#### Scenario: nonexistent-workspace-not-404
- GIVEN 一个随机的、不存在的 workspace UUID
- WHEN 成员身份合法的用户携带该 id 访问 workspace 详情
- THEN 返回 HTTP 403 而非 404，避免 workspace 存在性泄露
- AND 请求体中 `metadata.workspace_id` 非法时返回 HTTP 422

### Requirement: rbac-role-thresholds
系统 SHALL 以 owner(2) > admin(1) > member(0) 的等级比较 RBAC 门槛，低于门槛返回 403（detail 为「需要 {角色} 及以上角色」）；成员管理端点门槛为 ADMIN。

#### Scenario: member-blocked-from-admin-endpoints
- GIVEN OWNER 已将用户加入 workspace 并指定角色 `member`
- WHEN 该 MEMBER 查看 workspace 详情、成员列表与添加成员
- THEN 详情返回 HTTP 200，成员管理两个端点返回 HTTP 403

#### Scenario: admin-and-owner-can-manage-members
- GIVEN workspace 的 OWNER 令牌
- WHEN 按用户名添加成员并指定角色，再查看成员列表
- THEN 添加返回 HTTP 201，列表中该用户名对应的角色与指定值一致
- AND 目标用户名不存在时返回 HTTP 404

### Requirement: owner-role-escalation-guard
系统 MUST 拒绝任何通过添加/更新成员接口把角色指定为 `owner` 的请求（403，detail「不能通过此接口添加 OWNER」），即使操作者本人已是 ADMIN 或 OWNER。

#### Scenario: admin-cannot-promote-to-owner
- GIVEN OWNER 已将 `esc_admin` 提升为 ADMIN
- WHEN ADMIN 再次以 `{"role":"owner"}` 调用添加成员接口
- THEN 返回 HTTP 403
- AND `esc_admin` 在成员列表中的角色仍为 `admin`

### Requirement: workspace-list-isolation
系统 SHALL 让 GET `/api/workspaces` 仅返回当前用户拥有成员关系的 workspace（按 workspace_members 联表过滤），新用户在注册时自动获得唯一一个 OWNER 身份的个人空间。

#### Scenario: list-contains-only-joined-workspaces
- GIVEN 已分别注册并登录的两个用户
- WHEN 各自请求 GET `/api/workspaces`
- THEN 每个响应只含自身的 workspace
- AND A 的返回项集合中不含 B 的 workspace id

### Requirement: vector-store-workspace-partition
系统 MUST 让知识切片表携带冗余 `workspace_id` 列与索引，使向量近邻检索、BM25 语料装载与检索编排全部按该列分区，避免跨 workspace 召回。

#### Scenario: vector-search-scoped-by-workspace
- GIVEN 两个 workspace 各自已嵌入的知识切片
- WHEN 以 A 的 workspace id 执行向量召回与混合检索
- THEN SQL 条件含 `KnowledgeChunk.workspace_id == A` 且仅排除 embedding 为空的切片
- AND BM25 语料只装载 A 的在库切片分词结果

#### Scenario: retrieval-degrades-within-partition
- GIVEN A workspace 内的检索请求，且 embedding 或 rerank 服务调用失败
- WHEN 执行混合检索
- THEN 降级为纯 BM25 或 RRF 直排，候选仍限定在 A 的切片内
- AND 请求的操作范围不会扩展到其它 workspace 的切片

### Requirement: cross-workspace-resource-denial
系统 SHALL 使不属于当前 workspace 的会话资源在所有子资源端点（详情、消息历史、追加消息、删除，以及 body 携带 workspace_id 的对话流端点）上均不可读写。

#### Scenario: subresource-access-denied
- GIVEN 用户 B 与属于 A 的会话
- WHEN B 携带自己的 `workspace_id` 请求该会话详情、追加消息与删除
- THEN 三个请求均返回 HTTP 404

#### Scenario: using-foreign-workspace-forbidden
- GIVEN 用户 B 不是 A 的成员
- WHEN B 携带 A 的 `workspace_id` 请求会话列表或对话流
- THEN 返回 HTTP 403，错误码 `permission_denied`
- AND A 自身列表的 `total` 不受 B 的数据影响
