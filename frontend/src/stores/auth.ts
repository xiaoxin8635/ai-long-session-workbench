/**
 * 认证状态 store（Zustand + localStorage 持久化）。
 *
 * 职责：
 *   - 持有 user / 双令牌，localStorage 持久化（刷新页面不掉登录态）；
 *   - 初始化时调用 bindAuthHooks 与 HTTP 层对接（401 刷新/登出联动）；
 *   - 提供 login / registerAndLogin / logout / bootstrap 动作。
 */
import { create } from "zustand";
import { persist } from "zustand/middleware";
import * as authApi from "../api/auth";
import { bindAuthHooks } from "../api/client";
import type { Problem } from "../api/types";

/** 认证 store 状态与动作。 */
interface AuthState {
  /** 当前用户信息（未登录为 null）。 */
  user: authApi.UserRead | null;
  /** access token（30 分钟有效）。 */
  accessToken: string | null;
  /** refresh token（7 天有效）。 */
  refreshToken: string | null;

  /**
   * 登录：换取双令牌并拉取用户信息。
   *
   * @param username - 用户名。
   * @param password - 密码。
   * @throws Problem 登录失败（统一 401 文案）。
   */
  login: (username: string, password: string) => Promise<void>;

  /**
   * 注册并直接登录（注册成功后自动走登录流程）。
   *
   * @param payload - 注册信息。
   * @throws Problem 注册失败（如同名 409）。
   */
  registerAndLogin: (payload: authApi.RegisterPayload) => Promise<void>;

  /** 登出：清空本地令牌与用户信息。 */
  logout: () => void;

  /**
   * 应用启动时恢复登录态：有令牌则拉取 me 校验，失败静默登出。
   */
  bootstrap: () => Promise<void>;
}

export const useAuthStore = create<AuthState>()(
  persist(
    (set, get) => ({
      user: null,
      accessToken: null,
      refreshToken: null,

      login: async (username, password) => {
        const tokens = await authApi.login(username, password);
        set({ accessToken: tokens.access_token, refreshToken: tokens.refresh_token });
        const user = await authApi.me();
        set({ user });
      },

      registerAndLogin: async (payload) => {
        await authApi.register(payload);
        await get().login(payload.username, payload.password);
      },

      logout: () => {
        set({ user: null, accessToken: null, refreshToken: null });
      },

      bootstrap: async () => {
        if (!get().accessToken) {
          return;
        }
        try {
          const user = await authApi.me();
          set({ user });
        } catch {
          // me 失败（含 401 刷新失败已触发登出回调）时兜底清理
          set({ user: null, accessToken: null, refreshToken: null });
        }
      },
    }),
    {
      name: "echodesk-auth",
      partialize: (s) => ({ user: s.user, accessToken: s.accessToken, refreshToken: s.refreshToken }),
    }
  )
);

// HTTP 层 ↔ store 对接：令牌读写与登出回调（避免 client → store 循环 import）
bindAuthHooks({
  getAccessToken: () => useAuthStore.getState().accessToken,
  getRefreshToken: () => useAuthStore.getState().refreshToken,
  onTokensRefreshed: (access, refresh) =>
    useAuthStore.setState({ accessToken: access, refreshToken: refresh }),
  onAuthFailure: () => useAuthStore.getState().logout(),
});

/**
 * 从 Problem 或未知错误中提取面向用户的中文文案。
 *
 * @param err - 任意抛出值。
 * @returns 中文错误描述。
 */
export function errorMessage(err: unknown): string {
  if (err && typeof err === "object" && "detail" in err) {
    return (err as Problem).detail;
  }
  if (err instanceof Error) {
    return err.message;
  }
  return "未知错误，请重试";
}
