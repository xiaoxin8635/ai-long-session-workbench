/**
 * 用量页冒烟测试（EchoDesk 前端）。
 *
 * mock api/usage 与 api/workspaces，验证概览卡、区块拆分与按日趋势渲染。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../api/workspaces", () => ({
  listWorkspaces: vi.fn().mockResolvedValue([
    { id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "" },
  ]),
}));
vi.mock("../api/usage", () => ({
  fetchUsageSummary: vi.fn(),
}));

const { default: UsagePage } = await import("../pages/UsagePage");
const usageApi = await import("../api/usage");
const fetchUsageSummaryMock = vi.mocked(usageApi.fetchUsageSummary);

describe("UsagePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    fetchUsageSummaryMock.mockResolvedValue({
      workspace_id: "ws-1",
      days: 7,
      totals: {
        prompt_tokens: 10000,
        completion_tokens: 3000,
        memory_tokens: 2000,
        rag_tokens: 800,
        tool_tokens: 120,
      },
      sessions: 4,
      turns: 26,
      by_day: [
        { day: "2026-09-19", prompt_tokens: 6000, completion_tokens: 1800 },
        { day: "2026-09-20", prompt_tokens: 4000, completion_tokens: 1200 },
      ],
    });
  });

  it("渲染概览卡与趋势数据", async () => {
    render(<UsagePage />);
    expect(await screen.findByText("总 token")).toBeTruthy();
    expect(screen.getByText("13,000")).toBeTruthy();
    expect(screen.getByText("4")).toBeTruthy();
    expect(screen.getByText("26")).toBeTruthy();
    expect(screen.getByText(/09-19/)).toBeTruthy();
  });

  it("渲染区块拆分占比", async () => {
    render(<UsagePage />);
    expect(await screen.findByText("上下文区块拆分（占 prompt 比例）")).toBeTruthy();
    // 占比 span 由多个文本节点组成（"2,000" + " · " + "20.0" + "%"），
    // 用 textContent 精确匹配
    expect(
      screen.getByText((_, el) => el?.textContent === "2,000 · 20.0%")
    ).toBeTruthy();
    expect(
      screen.getByText((_, el) => el?.textContent === "120 · 1.2%")
    ).toBeTruthy();
  });
});
