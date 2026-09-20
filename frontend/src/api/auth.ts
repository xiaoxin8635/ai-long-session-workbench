/**
 * 认证 API（EchoDesk 前端）。
 *
 * 对接 memory-service `/api/auth/*`：
 *   register（注册并自动建个人 workspace）/ login（双令牌）/ refresh / me。
 */
import { api } from "./client";

/** 用户信息（GET /api/auth/me 响应）。 */
export interface UserRead {
  id: string;
  username: string;
  email: string | null;
  display_name: string | null;
  created_at: string;
}

/** 双令牌对（POST /api/auth/login 与 /refresh 响应）。 */
export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
}

/** 注册请求体。 */
export interface RegisterPayload {
  username: string;
  password: string;
  email?: string;
  display_name?: string;
}

/**
 * 注册新用户（自动创建个人 workspace 并绑定 owner）。
 *
 * @param payload - 用户名/密码/可选邮箱与显示名。
 * @returns 新用户信息（不含令牌，注册后需登录）。
 */
export async function register(payload: RegisterPayload): Promise<UserRead> {
  const { data } = await api.post<UserRead>("/api/auth/register", payload);
  return data;
}

/**
 * 用户名密码登录，换取双令牌。
 *
 * @param username - 用户名。
 * @param password - 密码。
 * @returns 双令牌对。
 */
export async function login(username: string, password: string): Promise<TokenPair> {
  const { data } = await api.post<TokenPair>("/api/auth/login", { username, password });
  return data;
}

/**
 * 用 refresh token 换取新的双令牌。
 *
 * @param refreshToken - 当前 refresh token。
 * @returns 新双令牌对。
 */
export async function refresh(refreshToken: string): Promise<TokenPair> {
  const { data } = await api.post<TokenPair>("/api/auth/refresh", {
    refresh_token: refreshToken,
  });
  return data;
}

/**
 * 获取当前登录用户信息。
 *
 * @returns 用户信息。
 */
export async function me(): Promise<UserRead> {
  const { data } = await api.get<UserRead>("/api/auth/me");
  return data;
}
