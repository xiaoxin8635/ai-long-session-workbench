/**
 * Vite 构建配置（EchoDesk 前端）。
 *
 * - dev 服务经 proxy 把 /api 与 /v1 转发到本机 memory-service(8100)，
 *   前端代码不感知后端地址；生产由 Nginx 同源反代（见 frontend/nginx.conf）。
 * - test 段配置 vitest：jsdom 环境 + 全局断言扩展 + setup 文件。
 */
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    // dev 端口 3200：3000 当前被 Open WebUI 占用（M-F6 退役后生产 web 容器接管 3000）
    port: 3200,
    proxy: {
      // target 用 127.0.0.1 而非 localhost：本机 wslrelay 曾占用 [::1]:8100，
      // Node 解析 localhost 优先 ::1 时请求会被劫持（见 docs/04 环境手册踩坑记录）
      "/api": { target: "http://127.0.0.1:8100", changeOrigin: true },
      // SSE 流式对话走 /v1；vite dev proxy 默认透传 chunk 不缓冲，无需特殊选项
      "/v1": { target: "http://127.0.0.1:8100", changeOrigin: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.{test,spec}.{ts,tsx}"],
    // 本机 WSL2 VM 常驻占用约 8GB，并行 worker 会偶发 OOM 崩溃（v3.2 实测），
    // 文件量小改为串行跑，稳定性优先
    fileParallelism: false,
  },
});
