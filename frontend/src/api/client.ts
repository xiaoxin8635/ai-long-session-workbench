/**
 * axios 实例与 JWT 拦截器（EchoDesk 前端 HTTP 层核心）。
 *
 * 职责：
 *   - 请求拦截：自动附加 `Authorization: Bearer <access>`；
 *   - 响应拦截：401 时用 refresh_token 单飞刷新（并发请求共享同一次刷新），
 *     成功后重放原请求，失败则触发登出回调；
 *   - 错误归一：后端 RFC 7807 problem+json 解析为 Problem 抛出。
 *
 * 与 auth store 的解耦：本模块不 import store（避免循环依赖），
 * store 初始化时调用 bindAuthHooks 注入 token 读写与登出回调。
 */
import axios, {
  AxiosError,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from "axios";
import { isProblem, type Problem } from "./types";

/** 后端基础地址：dev 走 vite proxy（同源相对路径），prod 由 Nginx 同源反代。 */
const BASE_URL = import.meta.env.VITE_API_BASE ?? "";

/** 请求携带的超时（毫秒）；SSE 流式请求单独走 lib/sse，不经此实例。 */
const DEFAULT_TIMEOUT_MS = 15_000;

/** auth 钩子集合：由 auth store 绑定，client 据此读写令牌。 */
interface AuthHooks {
  /** 读取当前 access token（未登录返回 null）。 */
  getAccessToken: () => string | null;
  /** 读取当前 refresh token（未登录返回 null）。 */
  getRefreshToken: () => string | null;
  /** 刷新成功后回写双令牌。 */
  onTokensRefreshed: (accessToken: string, refreshToken: string) => void;
  /** 刷新失败（refresh token 亦失效）时登出并跳转登录页。 */
  onAuthFailure: () => void;
}

let hooks: AuthHooks = {
  getAccessToken: () => null,
  getRefreshToken: () => null,
  onTokensRefreshed: () => undefined,
  onAuthFailure: () => undefined,
};

/**
 * 绑定 auth 钩子（auth store 初始化时调用一次）。
 *
 * @param next - store 提供的令牌读写与登出回调集合。
 */
export function bindAuthHooks(next: AuthHooks): void {
  hooks = next;
}

/** 进行中的刷新 Promise（单飞：并发 401 共享同一次 refresh 调用）。 */
let refreshInFlight: Promise<string> | null = null;

/**
 * 用 refresh token 换取新的双令牌，返回新的 access token。
 *
 * @returns 新 access token。
 * @throws 刷新失败（401/网络错误）时抛出，并触发 onAuthFailure 登出。
 */
async function refreshAccessToken(): Promise<string> {
  const refreshToken = hooks.getRefreshToken();
  if (!refreshToken) {
    hooks.onAuthFailure();
    throw new Error("无 refresh token，需重新登录");
  }
  try {
    // 用独立 axios 裸调用，避免走本实例拦截器造成递归刷新
    const resp = await axios.post<{ access_token: string; refresh_token: string }>(
      `${BASE_URL}/api/auth/refresh`,
      { refresh_token: refreshToken },
      { timeout: DEFAULT_TIMEOUT_MS }
    );
    hooks.onTokensRefreshed(resp.data.access_token, resp.data.refresh_token);
    return resp.data.access_token;
  } catch (err) {
    hooks.onAuthFailure();
    throw err instanceof Error ? err : new Error("刷新令牌失败");
  }
}

/** axios 实例：全部 REST API 调用共用。 */
export const api = axios.create({
  baseURL: BASE_URL,
  timeout: DEFAULT_TIMEOUT_MS,
});

// 请求拦截：附加 Bearer 令牌
api.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = hooks.getAccessToken();
  if (token) {
    config.headers.set("Authorization", `Bearer ${token}`);
  }
  return config;
});

// 响应拦截：401 单飞刷新后重放；错误归一为 Problem
api.interceptors.response.use(
  (response: AxiosResponse) => response,
  async (error: AxiosError) => {
    const config = error.config as (InternalAxiosRequestConfig & { _retry?: boolean }) | undefined;
    const isAuthEndpoint = config?.url?.includes("/api/auth/");
    const status = error.response?.status;

    // 401 且非认证端点且未重试过 → 尝试刷新后重放
    if (status === 401 && config && !config._retry && !isAuthEndpoint) {
      config._retry = true;
      if (refreshInFlight === null) {
        refreshInFlight = refreshAccessToken().finally(() => {
          refreshInFlight = null;
        });
      }
      try {
        const newToken = await refreshInFlight;
        config.headers.set("Authorization", `Bearer ${newToken}`);
        return api.request(config);
      } catch {
        // 刷新失败已在 refreshAccessToken 内触发登出，这里落回统一错误
      }
    }

    throw normalizeError(error);
  }
);

/**
 * 将 axios 错误归一为 Problem（优先解析后端 RFC 7807 结构）。
 *
 * @param error - axios 错误实例。
 * @returns 归一后的 Problem（网络错误给统一文案）。
 */
export function normalizeError(error: AxiosError): Problem {
  const data = error.response?.data as unknown;
  if (isProblem(data)) {
    return data;
  }
  if (error.code === "ECONNABORTED") {
    return { type: "timeout", title: "timeout", status: 0, detail: "请求超时，请稍后重试" };
  }
  return {
    type: "network_error",
    title: "network_error",
    status: error.response?.status ?? 0,
    detail: "网络异常，请检查服务是否可用",
  };
}
