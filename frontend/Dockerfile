# ============================================================
# EchoDesk 前端镜像（多阶段构建，M-F6）
#
#   Stage 1: Node 构建 TS/React 产物（tsc -b + vite build → dist/）
#   Stage 2: Nginx 托管静态文件，并同源反代 memory-service
#            （/api REST、/v1 聊天 SSE，SSE 关缓冲保流式）
#
# 构建加速：NPM_REGISTRY build-arg 指定 npm 镜像源（国内 npmmirror），
# 经 deploy/docker-compose.yml 的 build.args 从 deploy/.env 注入。
# ============================================================

# ---------- Stage 1：构建静态产物 ----------
FROM node:22-alpine AS build

# npm 镜像源（默认官方源；国内构建经 compose 传 npmmirror）
ARG NPM_REGISTRY=https://registry.npmjs.org

WORKDIR /app

# 先拷贝依赖清单再安装：清单未变时命中 Docker 层缓存，跳过安装
COPY package.json package-lock.json ./
RUN npm ci --registry=${NPM_REGISTRY}

# 再拷贝源码执行构建（tsc -b 类型检查 + vite build 产出 dist/）
COPY . .
RUN npm run build

# ---------- Stage 2：Nginx 运行时 ----------
FROM nginx:1.27-alpine

# 站点配置：静态托管 + SPA 回退 + /api、/v1 同源反代（SSE 关缓冲）
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 静态产物（vite build 输出，assets 带内容哈希）
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
