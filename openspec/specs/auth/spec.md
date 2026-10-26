## Purpose
定义 EchoDesk 认证域的现有行为契约：注册、登录、刷新、身份自省的后端语义，access（默认 30 分钟）与 refresh（默认 7 天）双令牌的类型隔离，以及前端 HTTP 层的单飞刷新、重放与登出跳转。本文件为已实现行为的 brownfield 基线。

## Requirements

### Requirement: user-registration-transaction
系统 SHALL 在单事务内完成注册：创建用户（bcrypt cost=12）、创建名为「{display_name 或 username} 的空间」的个人 workspace、写入 OWNER 成员关系；成功返回 201 且响应不含 password / password_hash；用户名冲突返回 409，非法用户名或过短密码返回 422。

#### Scenario: register-creates-user-workspace-and-owner
- GIVEN 新用户名 `alice` 与合规密码（8~72 字符）
- WHEN POST `/api/auth/register`
- THEN 返回 HTTP 201，body 含 id / username / email / display_name / created_at
- AND 登录该账号后 GET `/api/workspaces` 恰返回 1 项，name 为「Alice 的空间」且 owner_id 为用户 id

#### Scenario: register-duplicate-username-conflict
- GIVEN 已存在用户名为 `bob` 的账号
- WHEN 再次以 `bob` 调用 POST `/api/auth/register`
- THEN 返回 HTTP 409
- AND 响应 `title` 为 `conflict`

### Requirement: login-dual-token-pair
系统 SHALL 在用户名与密码校验通过后签发 HS256 JWT 令牌对（载荷含 sub / type / iat / exp），TokenPair 的 token_type 恒为 `bearer`，access 有效期取 `jwt_expire_minutes`（默认 30 分钟）、refresh 取 `refresh_expire_days`（默认 7 天）。

#### Scenario: login-success-issues-pair
- GIVEN 已注册用户 `dave` 且密码正确
- WHEN POST `/api/auth/login`
- THEN 返回 HTTP 200，access_token 与 refresh_token 均非空，token_type 为 `bearer`
- AND 携带该 access 令牌访问 GET `/api/auth/me` 返回 200 且 username 为 `dave`

#### Scenario: login-failure-uniform-401
- GIVEN 已注册用户 `erin`
- WHEN 分别以错误密码、以及不存在的用户名 `ghost-user` 登录
- THEN 两次均返回 HTTP 401，错误码为 `invalid_credentials`
- AND 两次响应 `detail` 完全一致，用户被禁用时同样返回该 401

### Requirement: token-verification-and-type-isolation
系统 MUST 在受保护端点的 `get_current_user` 依赖中要求 Bearer access 令牌，并在解码时校验 type 与 exp：令牌缺失、签名非法、已过期、类型不符或用户被禁用一律返回 401（错误码 `unauthorized`），不泄露用户存在性。

#### Scenario: access-token-rejected-as-refresh
- GIVEN 登录后持有的 access_token
- WHEN 以其调用 POST `/api/auth/refresh`
- THEN 返回 HTTP 401

#### Scenario: expired-access-token-rejected
- GIVEN 手工签发且 exp 已过期的 access 令牌
- WHEN 携带该令牌访问 GET `/api/workspaces`
- THEN 返回 HTTP 401

#### Scenario: missing-or-malformed-token-rejected
- GIVEN 未携带 Authorization 头，或携带 Bearer `not-a-jwt`
- WHEN 访问 GET `/api/workspaces`
- THEN 返回 HTTP 401

### Requirement: rfc7807-problem-json-envelope
系统 MUST 将业务异常统一序列化为 RFC 7807 problem 结构：type 为 `https://echodesk.errors/{code}`、title 为错误码、status 为 HTTP 状态码、detail 为中文描述、trace_id 为链路标识，并记录含 path / code / status 的 `app_error` 日志。

#### Scenario: business-error-response-shape
- GIVEN 请求触发任意 AppError（如 conflict / invalid_credentials / unauthorized）
- WHEN 服务端返回错误响应
- THEN 响应体同时包含 type / title / status / detail / trace_id 五个字段
- AND `title` 等于机器可读错误码，`status` 与实际 HTTP 状态码一致

#### Scenario: auth-error-codes-mapped
- GIVEN 认证与鉴权失败场景
- WHEN 分别触发登录失败、令牌无效、越权访问
- THEN 错误响应 `title` 分别为 `invalid_credentials`（401）、`unauthorized`（401）、`permission_denied`（403）

### Requirement: refresh-token-renewal
系统 SHALL 在 POST `/api/auth/refresh` 校验 refresh 类型令牌及用户启用状态后签发新令牌对；旧令牌仅因自身过期而失效（不做主动吊销）。

#### Scenario: valid-refresh-rotates-pair
- GIVEN 用户 `frank` 登录后持有的 refresh_token
- WHEN 调用 POST `/api/auth/refresh`
- THEN 返回 HTTP 200 与新令牌对
- AND 新 access_token 可成功访问 GET `/api/auth/me`

#### Scenario: malformed-refresh-rejected
- GIVEN 字符串 `not-a-jwt`
- WHEN 以其作为 refresh_token 调用 POST `/api/auth/refresh`
- THEN 返回 HTTP 401

### Requirement: frontend-single-flight-refresh-replay
前端拦截器 MUST 在收到 401 时以单飞 Promise 共享一次刷新：并发 401 只发出一次 POST `/api/auth/refresh`，成功后用新 access 令牌重放原请求，且每个请求最多重试一次；`/api/auth/` 端点不参与刷新重试。

#### Scenario: concurrent-401-shares-one-refresh
- GIVEN 多个并发请求同时返回 401，且 refresh_token 仍有效
- WHEN 拦截器处理这些响应
- THEN 仅发出一次刷新请求
- AND 所有请求均以新令牌重放并返回各自业务响应，同时回调写回新双令牌

#### Scenario: refresh-failure-logs-out
- GIVEN refresh_token 已失效且某端点返回 401
- WHEN 拦截器尝试刷新失败
- THEN 触发 `onAuthFailure` 清空本地 user / accessToken / refreshToken
- AND 原请求抛出归一后的 Problem 且不再重试

#### Scenario: auth-endpoints-excluded-from-replay
- GIVEN `/api/auth/` 下的端点返回 401
- WHEN 拦截器处理该响应
- THEN 不触发刷新与重放，直接抛出 Problem

### Requirement: frontend-session-persistence-and-login-redirect
前端 SHALL 以 localStorage（键 `echodesk-auth`）持久化 user 与双令牌，启动时用 `/api/auth/me` 校验登录态、失败则静默清空令牌；路由守卫在 accessToken 缺失时重定向到 `/login?redirect=<编码后的原路径与查询串>`。

#### Scenario: bootstrap-restores-session
- GIVEN localStorage 中存在持久化的 accessToken
- WHEN 应用启动执行 bootstrap 且 `/api/auth/me` 成功
- THEN 回填 user 并保持已登录状态，刷新页面不掉登录态

#### Scenario: guard-redirects-to-login
- GIVEN 当前无 accessToken（含 me 校验失败被清空的情况）
- WHEN 访问受保护路由
- THEN 重定向至 `/login` 并以 redirect 参数携带原路径与查询串
