/**
 * 登录页交互单测（EchoDesk 前端）。
 *
 * 覆盖：Tab 切换、登录提交调用 store 动作、错误文案展示、成功写入令牌。
 * 路由依赖用 MemoryRouter 包裹；认证 API 层 mock，store 走真实逻辑。
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

// mock 认证 API（不发真实请求）
vi.mock("../api/auth", () => ({
  register: vi.fn(),
  login: vi.fn(),
  me: vi.fn().mockResolvedValue({ id: "u1", username: "alice", email: null, display_name: null }),
  refresh: vi.fn(),
}));

const { default: LoginPage } = await import("../pages/Login");
const { useAuthStore } = await import("../stores/auth");
const authApi = await import("../api/auth");

/** 以路由包裹渲染登录页。 */
function renderLogin(): void {
  render(
    <MemoryRouter initialEntries={["/login"]}>
      <LoginPage />
    </MemoryRouter>
  );
}

describe("LoginPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({ user: null, accessToken: null, refreshToken: null });
    vi.mocked(authApi.login).mockImplementation(async () => {
      throw { title: "invalid_credentials", status: 401, detail: "用户名或密码错误" };
    });
  });

  it("默认登录模式，可切换到注册（出现显示名输入框）", async () => {
    renderLogin();
    expect(screen.getByPlaceholderText(/用户名/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "注册" }));
    expect(screen.getByPlaceholderText(/显示名/)).toBeInTheDocument();
  });

  it("登录失败展示后端 problem detail 文案", async () => {
    renderLogin();
    await userEvent.type(screen.getByPlaceholderText(/用户名/), "alice");
    await userEvent.type(screen.getByPlaceholderText(/密码/), "password-8");
    await userEvent.click(screen.getByRole("button", { name: "登录工作台" }));

    await waitFor(() => {
      expect(screen.getByText("用户名或密码错误")).toBeInTheDocument();
    });
  });

  it("登录成功写入双令牌", async () => {
    vi.mocked(authApi.login).mockResolvedValue({
      access_token: "at-1",
      refresh_token: "rt-1",
      token_type: "bearer",
    });
    renderLogin();
    await userEvent.type(screen.getByPlaceholderText(/用户名/), "alice");
    await userEvent.type(screen.getByPlaceholderText(/密码/), "password-8");
    await userEvent.click(screen.getByRole("button", { name: "登录工作台" }));

    await waitFor(() => {
      expect(useAuthStore.getState().accessToken).toBe("at-1");
      expect(useAuthStore.getState().refreshToken).toBe("rt-1");
    });
  });
});
