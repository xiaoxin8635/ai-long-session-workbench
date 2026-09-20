/**
 * 认证 store 单测（EchoDesk 前端）。
 *
 * 通过 vi.mock 替换 api/auth 层，只测 store 动作编排逻辑
 * （login 换令牌+拉用户、registerAndLogin 串注册与登录、logout 清态）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

// mock 掉 HTTP 层（不发真实请求）
vi.mock("../api/auth", () => ({
  register: vi.fn().mockResolvedValue({ id: "u1", username: "alice" }),
  login: vi.fn().mockResolvedValue({
    access_token: "at-1",
    refresh_token: "rt-1",
    token_type: "bearer",
  }),
  me: vi.fn().mockResolvedValue({ id: "u1", username: "alice", email: null, display_name: "Alice" }),
  refresh: vi.fn(),
}));

const { useAuthStore } = await import("../stores/auth");
const authApi = await import("../api/auth");

describe("auth store", () => {
  beforeEach(() => {
    useAuthStore.setState({ user: null, accessToken: null, refreshToken: null });
    vi.clearAllMocks();
  });

  it("login 换取双令牌并拉取用户信息", async () => {
    await useAuthStore.getState().login("alice", "password-8");
    expect(authApi.login).toHaveBeenCalledWith("alice", "password-8");
    expect(useAuthStore.getState().accessToken).toBe("at-1");
    expect(useAuthStore.getState().refreshToken).toBe("rt-1");
    expect(useAuthStore.getState().user?.username).toBe("alice");
  });

  it("registerAndLogin 先注册后登录", async () => {
    await useAuthStore.getState().registerAndLogin({ username: "alice", password: "password-8" });
    expect(authApi.register).toHaveBeenCalledWith({ username: "alice", password: "password-8" });
    expect(authApi.login).toHaveBeenCalledWith("alice", "password-8");
    expect(useAuthStore.getState().user).not.toBeNull();
  });

  it("logout 清空全部认证态", () => {
    useAuthStore.setState({
      user: { id: "u1", username: "alice", email: null, display_name: null, created_at: "" },
      accessToken: "at",
      refreshToken: "rt",
    });
    useAuthStore.getState().logout();
    expect(useAuthStore.getState()).toMatchObject({
      user: null,
      accessToken: null,
      refreshToken: null,
    });
  });

  it("bootstrap 无令牌时不动，有令牌时拉取 me", async () => {
    await useAuthStore.getState().bootstrap();
    expect(authApi.me).not.toHaveBeenCalled();

    useAuthStore.setState({ accessToken: "at-kept", refreshToken: "rt" });
    await useAuthStore.getState().bootstrap();
    expect(authApi.me).toHaveBeenCalledTimes(1);
    expect(useAuthStore.getState().user?.username).toBe("alice");
  });
});
