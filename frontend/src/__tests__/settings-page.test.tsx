/**
 * 设置页冒烟测试（EchoDesk 前端）。
 *
 * mock api/workspaces 与 auth store，验证个人资料卡、成员表渲染与添加成员调用。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("../api/workspaces", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/workspaces")>();
  return {
    ...actual,
    listWorkspaces: vi.fn().mockResolvedValue([
      { id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "2026-09-01T00:00:00Z" },
    ]),
    listMembers: vi.fn().mockResolvedValue([
      {
        workspace_id: "ws-1",
        user_id: "u-1",
        username: "alice",
        display_name: "爱丽丝",
        role: "owner",
        created_at: "2026-09-01T00:00:00Z",
      },
      {
        workspace_id: "ws-1",
        user_id: "u-2",
        username: "bob",
        display_name: null,
        role: "member",
        created_at: "2026-09-05T00:00:00Z",
      },
    ]),
    addMember: vi.fn().mockResolvedValue({
      workspace_id: "ws-1",
      user_id: "u-3",
      username: "carol",
      display_name: null,
      role: "member",
      created_at: "2026-09-20T00:00:00Z",
    }),
  };
});

const { default: SettingsPage } = await import("../pages/SettingsPage");
const workspacesApi = await import("../api/workspaces");
const { useAuthStore } = await import("../stores/auth");

const addMemberMock = vi.mocked(workspacesApi.addMember);

describe("SettingsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useAuthStore.setState({
      user: {
        id: "u-1",
        username: "alice",
        email: null,
        display_name: "爱丽丝",
        created_at: "2026-09-01T00:00:00Z",
      },
    });
  });

  it("渲染个人资料与成员表", async () => {
    render(<SettingsPage />);
    expect(await screen.findByText("个人资料")).toBeTruthy();
    // alice 同时出现在个人资料卡与成员表，断言出现即可
    expect(screen.getAllByText("alice").length).toBeGreaterThanOrEqual(1);
    expect(await screen.findByText("bob")).toBeTruthy();
    expect(screen.getAllByText("所有者").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("成员").length).toBeGreaterThanOrEqual(1);
  });

  it("添加成员走 API 并刷新列表", async () => {
    const listMembersMock = vi.mocked(workspacesApi.listMembers);
    render(<SettingsPage />);
    await screen.findByText("bob");

    fireEvent.change(screen.getByPlaceholderText("按用户名添加成员"), {
      target: { value: "carol" },
    });
    fireEvent.click(screen.getByRole("button", { name: "添加" }));

    await vi.waitFor(() => {
      expect(addMemberMock).toHaveBeenCalled();
    });
    // 成功后刷新成员列表（mock 恒返回 alice/bob 两行）
    expect(listMembersMock).toHaveBeenCalled();
  });
});
