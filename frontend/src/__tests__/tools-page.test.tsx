/**
 * 工具页冒烟测试（EchoDesk 前端）。
 *
 * mock api/tools 层，验证工具清单与调用审计渲染；执行/确认交互逻辑由
 * 实机探针覆盖（probe_mf3.py）。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

vi.mock("../api/tools", () => ({
  listTools: vi.fn(),
  listToolCalls: vi.fn(),
  executeTool: vi.fn(),
  confirmToolCall: vi.fn(),
}));

vi.mock("../api/mcp", () => ({
  listMcpServers: vi.fn(),
  addMcpServer: vi.fn(),
  removeMcpServer: vi.fn(),
}));

const { default: ToolsPage } = await import("../pages/ToolsPage");
const toolsApi = await import("../api/tools");
const mcpApi = await import("../api/mcp");
const { useChatStore } = await import("../stores/chat");

const listToolsMock = vi.mocked(toolsApi.listTools);
const listToolCallsMock = vi.mocked(toolsApi.listToolCalls);
const listMcpServersMock = vi.mocked(mcpApi.listMcpServers);
const removeMcpServerMock = vi.mocked(mcpApi.removeMcpServer);

describe("ToolsPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    // 预置 workspace，页面不再触发 bootstrap（不触 api/workspaces/sessions）
    useChatStore.setState({ workspaceId: "ws-1", sessions: [], error: null });
    listToolsMock.mockResolvedValue([
      {
        name: "web.fetch",
        description: "抓取指定 URL 的网页正文",
        risk: "external",
        args_schema: {},
      },
      {
        name: "todo.create",
        description: "创建待办任务",
        risk: "write",
        args_schema: {},
      },
    ]);
    listMcpServersMock.mockResolvedValue([
      {
        id: "7c9e6679-7425-40de-a5fd-000000000002",
        name: "demo-mcp",
        transport: "http",
        url: "http://demo:9000/mcp",
        command: null,
        args: [],
        enabled: true,
        source: "user",
        created_at: "2026-09-23T10:00:00Z",
        connected: true,
        tools: ["mcp.demo-mcp.echo"],
      },
    ]);
    listToolCallsMock.mockResolvedValue([
      {
        id: "9d2b0f29-8ac1-4d5f-b7e8-000000000001",
        session_id: null,
        tool_name: "web.fetch",
        risk_level: "external",
        args: { url: "https://example.com" },
        status: "success",
        approver_id: null,
        result_digest: "status_code=200",
        created_at: "2026-09-20T10:00:00Z",
      },
    ]);
  });

  it("渲染工具清单与风险徽标", async () => {
    render(<ToolsPage />);
    // 同名文本会同时出现在清单卡片与下拉选项中，用 findAll 断言至少渲染
    expect((await screen.findAllByText("web.fetch")).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("todo.create").length).toBeGreaterThanOrEqual(1);
    // external 与 write 徽标各出现
    expect(screen.getAllByText("需确认").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("写入").length).toBeGreaterThanOrEqual(1);
  });

  it("渲染调用审计表", async () => {
    render(<ToolsPage />);
    expect(await screen.findByText("status_code=200")).toBeTruthy();
    expect(screen.getByText("调用审计")).toBeTruthy();
    expect(screen.getByText("刷新")).toBeTruthy();
  });

  it("渲染 MCP 服务管理并支持热移除", async () => {
    removeMcpServerMock.mockResolvedValue(undefined);
    render(<ToolsPage />);
    expect(await screen.findByText("demo-mcp")).toBeTruthy();
    expect(screen.getByText("已连接 · 1 工具")).toBeTruthy();
    fireEvent.click(screen.getByLabelText("移除 demo-mcp"));
    await waitFor(() => expect(removeMcpServerMock).toHaveBeenCalledWith("demo-mcp"));
  });
});
