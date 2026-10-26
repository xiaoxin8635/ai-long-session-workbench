## Purpose
定义 EchoDesk 部署拓扑的已实现行为契约：五服务 compose 编排与端口、依赖与健康检查、Nginx 同源反代的 REST 与 SSE 契约、静态托管与前端镜像构建、dev 代理（brownfield 基线）。

## Requirements

### Requirement: compose-service-topology
系统 SHALL 以 compose 项目 `ai-long-workbench` 编排核心五服务：postgres（pgvector/pgvector:pg16）、redis（redis:7-alpine，AOF）、embedding（infinity，bge-m3 + bge-reranker-large）、memory-service、web。

#### Scenario: five-services-port-mapping
- GIVEN 未覆盖端口变量的默认部署
- WHEN compose 启动
- THEN 宿主端口依次为 web 3000、memory-service 8100、postgres 5432、redis 6379、embedding 7997
- AND 五服务同处 workbench-net 桥接网络且 restart 为 unless-stopped

#### Scenario: langfuse-profile-opt-in
- GIVEN 未启用 observability profile
- WHEN 执行 `docker compose up -d`
- THEN langfuse-web/worker/postgres/redis/clickhouse/minio 均不启动
- AND 启用该 profile 时 Langfuse Web 映射宿主 3001（3000 已被前端容器占用）

### Requirement: dependency-and-healthcheck-ordering
系统 SHALL 以健康检查编排启动顺序：memory-service 依赖 postgres 与 redis 的 service_healthy，web 依赖 memory-service。

#### Scenario: base-services-gated-on-health
- GIVEN postgres 与 redis 各自配置了健康检查
- WHEN 启动栈
- THEN memory-service 等待两者均 healthy 后才启动
- AND embedding 不设 healthcheck，其不可用由业务侧自带降级

#### Scenario: web-healthcheck-over-ipv4
- GIVEN web 容器已监听 80
- WHEN 健康检查执行 `wget -q -O- http://127.0.0.1/`
- THEN 检查间隔 15 秒、超时 5 秒、重试 5 次、启动宽限 5 秒
- AND 显式使用 127.0.0.1 以避开容器内 localhost 优先解析 [::1] 的问题

### Requirement: nginx-rest-proxy
系统 SHALL 在容器内 80 端口同源反代 REST：`/api/` 转发到 memory-service:8100 并保留客户端信息头。

#### Scenario: rest-read-timeout-120s
- GIVEN 知识库大文件在请求内同步解析切片
- WHEN 经 `/api/` 上传或请求
- THEN proxy_read_timeout 为 120s 以留足处理时间
- AND 转发时带上 Host、X-Real-IP、X-Forwarded-For 与 X-Forwarded-Proto

#### Scenario: healthz-end-to-end
- GIVEN 外部探活请求
- WHEN 访问 `= /healthz`
- THEN 透传到 memory-service 并关闭访问日志
- AND 可用于验证「Nginx → memory-service」链路

### Requirement: nginx-sse-streaming-contract
系统 MUST 为 `/v1/` 聊天 SSE 关闭缓冲与缓存、使用 HTTP/1.1 长连接，并刻意排除 gzip 对 event-stream 的压缩。

#### Scenario: buffering-off-and-600s-timeout
- GIVEN 流式聊天请求经 `/v1/` 转发
- WHEN LLM 生成或工具执行期间流长时间静默
- THEN proxy_read_timeout 为 600s 且 proxy_buffering、proxy_cache 均关闭
- AND 使用 HTTP/1.1 并清空 Connection 头，后端返回 `X-Accel-Buffering: no` 时同样不缓冲

#### Scenario: gzip-excludes-event-stream
- GIVEN 站点开启了 gzip
- WHEN 响应类型为 text/event-stream
- THEN 不对其压缩（gzip_types 仅含 text/css、application/javascript、application/json、image/svg+xml）
- AND gzip_min_length 为 1024

### Requirement: spa-static-hosting
系统 SHALL 托管 vite 构建产物：root 为 /usr/share/nginx/html，SPA 深链接回退 index.html，哈希资源长缓存。

#### Scenario: deep-link-fallback
- GIVEN 用户刷新 react-router 深链接
- WHEN Nginx 未命中静态文件
- THEN 回退到 /index.html
- AND listen 同时覆盖 80 与 [::]:80

#### Scenario: assets-cache-and-body-limit
- GIVEN 带内容哈希的 /assets/ 资源与大体积上传
- WHEN 请求资源或提交上传
- THEN 资源带 30d expires 与 `public, immutable`，且 client_max_body_size 为 25m
- AND /assets/ 未命中时直接返回 404 而非回退

### Requirement: frontend-image-and-dev-proxy
系统 SHALL 以多阶段构建产出前端镜像，并在 dev 环境用 vite proxy 转发到本机 memory-service。

#### Scenario: two-stage-build-and-nginx-runtime
- GIVEN 构建上下文为 frontend 目录
- WHEN 构建镜像
- THEN node:22-alpine 阶段先 `npm ci --registry=${NPM_REGISTRY}` 再 `npm run build`
- AND nginx:1.27-alpine 阶段把 nginx.conf 安装到 /etc/nginx/conf.d/default.conf、dist 拷到 /usr/share/nginx/html 并 EXPOSE 80

#### Scenario: dev-port-3200-and-loopback-target
- GIVEN 本地并行开发
- WHEN 启动 vite dev server
- THEN 监听 3200（生产 3000 由 web 容器占用）
- AND `/api` 与 `/v1` 均代理到 http://127.0.0.1:8100（避免 localhost 解析到 [::1] 被劫持）
