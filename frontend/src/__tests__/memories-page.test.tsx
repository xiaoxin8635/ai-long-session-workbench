/**
 * 记忆工作台页冒烟测试（EchoDesk 前端）。
 *
 * mock api 层，验证过滤条、列表卡片、冲突置顶提示条与检索测试区渲染。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../api/workspaces", () => ({
  listWorkspaces: vi.fn().mockResolvedValue([
    { id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "" },
  ]),
}));
vi.mock("../api/memories", () => ({
  listMemories: vi.fn(),
  getMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  resolveMemory: vi.fn(),
  searchMemories: vi.fn(),
}));

const { default: MemoriesPage } = await import("../pages/MemoriesPage");
const memoriesApi = await import("../api/memories");
import type { MemoryItem } from "../api/memories";

const listMemoriesMock = vi.mocked(memoriesApi.listMemories);

/**
 * 构造记忆条目。
 *
 * @param id - ID。
 * @param patch - 覆盖字段。
 * @returns MemoryItem 形状对象。
 */
function mkMemory(id: string, patch: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id,
    memory_type: "semantic",
    key: `fact.test.${id}`,
    content: "测试内容",
    confidence: 0.9,
    importance: 0.8,
    status: "active",
    version: 1,
    source_session_id: null,
    source_message_ids: [],
    supersedes_id: null,
    expires_at: null,
    hit_count: 0,
    last_hit_at: null,
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
    ...patch,
  };
}

describe("MemoriesPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listMemoriesMock.mockResolvedValue({
      items: [
        mkMemory("m-c", { key: "fact.team", content: "团队代号猎鹰", status: "conflicted" }),
        mkMemory("m-a", { key: "pref.lang", content: "偏好 Python", memory_type: "procedural" }),
      ],
      total: 2,
    });
  });

  it("渲染列表卡片与徽标", async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText("fact.team")).toBeTruthy();
    expect(screen.getByText("pref.lang")).toBeTruthy();
    expect(screen.getAllByText("冲突待裁决").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("偏好").length).toBeGreaterThanOrEqual(1);
  });

  it("冲突条目存在时显示置顶提示条", async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText(/1 条冲突待裁决/)).toBeTruthy();
  });

  it("渲染过滤条与检索测试区", async () => {
    render(<MemoriesPage />);
    expect(await screen.findByText("检索测试（与对话链路同检索器）")).toBeTruthy();
    expect(screen.getByText("全部类型")).toBeTruthy();
    expect(screen.getByText("全部状态")).toBeTruthy();
  });
});
