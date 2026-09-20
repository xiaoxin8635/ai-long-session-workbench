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
    // 本机 WSL2 VM 常驻占用约 8GB（空闲常 <4GB），并行 worker 会偶发 OOM：
    // ①fileParallelism: false 串行跑文件；②pool 用 forks（子进程）而非默认
    // threads——worker 线程与主进程共享地址空间，内存紧张时 V8 isolate cage
    // 预留失败直接 "Zone Allocation failed"（--max-old-space-size 也无效）；
    // ③execArgv 显式指定子进程堆形——内存吃紧时 V8 会把 semi-space 压到
    // 极小值，偶发 "Committing semi space failed"，固定参数消除抖动。
    // 非代码缺陷，环境约束，见 docs/04。
    fileParallelism: false,
    pool: "forks",
    // 恒定只保留 1 个 fork 子进程（默认会按 CPU 数预建进程池，空闲进程
    // 也各占一份 node+jsdom 内存），把套件常驻内存压到最低
    minWorkers: 1,
    maxWorkers: 1,
    poolOptions: {
      forks: {
        execArgv: ["--max-semi-space-size=64", "--max-old-space-size=1536"],
      },
    },
  },
});
