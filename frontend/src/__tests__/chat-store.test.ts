/**
 * 聊天 store 单测（EchoDesk 前端）。
 *
 * mock 三类 API 模块（workspaces/sessions/chat），验证：
 * bootstrap 初始化、selectSession 历史回放过滤、sendMessage 流式状态机
 * （乐观追加 → 增量回填 → citations 落地 / 失败标记）与流式中防重发。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/workspaces", () => ({
  listWorkspaces: vi.fn(),
}));
vi.mock("../api/sessions", () => ({
  listSessions: vi.fn(),
  listMessages: vi.fn(),
  createSession: vi.fn(),
  updateSession: vi.fn(),
  deleteSession: vi.fn(),
}));
vi.mock("../api/chat", () => ({
  streamChat: vi.fn(),
  streamResume: vi.fn(),
}));

const { useChatStore } = await import("../stores/chat");
const chatApi = await import("../api/chat");
const sessionsApi = await import("../api/sessions");
const workspacesApi = await import("../api/workspaces");
import type { Problem } from "../api/types";
import type { SessionRead } from "../api/sessions";

const listWorkspacesMock = vi.mocked(workspacesApi.listWorkspaces);
const listSessionsMock = vi.mocked(sessionsApi.listSessions);
const listMessagesMock = vi.mocked(sessionsApi.listMessages);
const streamChatMock = vi.mocked(chatApi.streamChat);
const streamResumeMock = vi.mocked(chatApi.streamResume);

/**
 * 构造 workspace mock 返回。
 *
 * @returns 单元素 workspace 数组。
 */
function mkWorkspaces(): { id: string; name: string; owner_id: string; created_at: string }[] {
  return [{ id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "2026-01-01T00:00:00Z" }];
}

/**
 * 构造会话对象。
 *
 * @param id - 会话 ID。
 * @param title - 标题。
 * @returns SessionRead 形状对象。
 */
function mkSession(id: string, title: string): SessionRead {
  return {
    id,
    workspace_id: "ws-1",
    user_id: "u-1",
    title,
    status: "active",
    token_total: 0,
    last_message_at: null,
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

/** 重置 store 至初始态（模块级单例，用例间隔离）。 */
function resetStore(): void {
  useChatStore.setState({
    workspaceId: null,
    sessions: [],
    activeSessionId: null,
    messages: [],
    streaming: false,
    error: null,
  });
}

/** 初始化 workspace 与空会话列表。 */
async function bootstrapWithEmptySessions(): Promise<void> {
  listWorkspacesMock.mockResolvedValue(mkWorkspaces());
  listSessionsMock.mockResolvedValue({ items: [], total: 0 });
  await useChatStore.getState().bootstrap();
}

describe("chat store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore();
  });

  it("bootstrap 拉取 workspace 与会话列表", async () => {
    listWorkspacesMock.mockResolvedValue(mkWorkspaces());
    listSessionsMock.mockResolvedValue({ items: [mkSession("s-1", "第一会话")], total: 1 });

    await useChatStore.getState().bootstrap();

    expect(useChatStore.getState().workspaceId).toBe("ws-1");
    expect(useChatStore.getState().sessions).toHaveLength(1);
    expect(listSessionsMock).toHaveBeenCalledWith("ws-1");
  });

  it("selectSession 过滤 tool/system 消息后回放历史", async () => {
    listWorkspacesMock.mockResolvedValue(mkWorkspaces());
    listSessionsMock.mockResolvedValue({ items: [mkSession("s-1", "第一会话")], total: 1 });
    listMessagesMock.mockResolvedValue({
      items: [
        { id: "m1", session_id: "s-1", role: "user", content: "问", token_count: 1, version: 1, metadata: {}, created_at: "" },
        { id: "m2", session_id: "s-1", role: "tool", content: "工具输出", token_count: 1, version: 1, metadata: {}, created_at: "" },
        { id: "m3", session_id: "s-1", role: "assistant", content: "答", token_count: 1, version: 1, metadata: {}, created_at: "" },
      ],
      total: 3,
    });
    await useChatStore.getState().bootstrap();

    await useChatStore.getState().selectSession("s-1");

    expect(useChatStore.getState().activeSessionId).toBe("s-1");
    expect(useChatStore.getState().messages.map((m) => m.key)).toEqual(["m1", "m3"]);
  });

  it("sendMessage 流式回填并落地 citations 与会话 ID", async () => {
    await bootstrapWithEmptySessions();
    streamChatMock.mockImplementation(async (params) => {
      params.handlers.onSessionId("s-new");
      params.handlers.onDelta("你");
      params.handlers.onDelta("好");
      params.handlers.onFinish({
        citations: [
          { chunk_id: "c1", file_id: "f1", filename: "a.md", chunk_index: 0, score: 0.9 },
        ],
        pendingConfirmation: null,
      });
    });

    await useChatStore.getState().sendMessage("你好");

    expect(streamChatMock).toHaveBeenCalledTimes(1);
    const arg = streamChatMock.mock.calls[0]![0];
    expect(arg.workspaceId).toBe("ws-1");
    expect(arg.sessionId).toBeUndefined(); // 草稿态：由服务端自动建会话
    expect(arg.content).toBe("你好");

    const state = useChatStore.getState();
    expect(state.activeSessionId).toBe("s-new");
    expect(state.streaming).toBe(false);
    expect(state.messages[0]).toMatchObject({ role: "user", content: "你好" });
    expect(state.messages[1]!.content).toBe("你好");
    expect(state.messages[1]!.citations).toHaveLength(1);
    expect(state.messages[1]!.failed).toBeUndefined();
  });

  it("流式期间拒绝重复发送", async () => {
    await bootstrapWithEmptySessions();
    let release: (() => void) | undefined;
    streamChatMock.mockImplementation(
      (_params) =>
        new Promise<void>((resolve) => {
          release = resolve;
        })
    );

    const first = useChatStore.getState().sendMessage("第一");
    expect(useChatStore.getState().streaming).toBe(true);

    await useChatStore.getState().sendMessage("第二");
    expect(streamChatMock).toHaveBeenCalledTimes(1);

    release!();
    await first;
    expect(useChatStore.getState().streaming).toBe(false);
  });

  it("上游错误事件标记失败并展示错误条", async () => {
    await bootstrapWithEmptySessions();
    streamChatMock.mockImplementation(async (params) => {
      params.handlers.onDelta("部分");
      params.handlers.onError("上游超时", "upstream_error");
    });

    await useChatStore.getState().sendMessage("问");

    const state = useChatStore.getState();
    const assistant = state.messages.find((m) => m.role === "assistant");
    expect(assistant?.failed).toBe(true);
    expect(state.error).toBe("上游超时");
    expect(state.streaming).toBe(false);
  });

  it("请求层异常（Problem）同样标记失败", async () => {
    await bootstrapWithEmptySessions();
    const problem: Problem = {
      type: "https://echodesk.errors/internal_error",
      title: "internal_error",
      status: 500,
      detail: "服务内部错误",
      trace_id: "tr-1",
    };
    streamChatMock.mockRejectedValue(problem);

    await useChatStore.getState().sendMessage("问");

    const state = useChatStore.getState();
    const assistant = state.messages.find((m) => m.role === "assistant");
    expect(assistant?.failed).toBe(true);
    expect(state.error).toBe("服务内部错误");
    expect(state.streaming).toBe(false);
  });

  it("sendMessage 收到 tool_call 事件时挂到助手消息", async () => {
    await bootstrapWithEmptySessions();
    streamChatMock.mockImplementation(async (params) => {
      params.handlers.onSessionId("s-t");
      params.handlers.onToolCall({
        call_id: "c-1",
        tool: "web.fetch",
        args: { url: "https://example.com" },
        risk: "external",
      });
    });

    await useChatStore.getState().sendMessage("抓网页");

    const assistant = useChatStore.getState().messages[1]!;
    expect(assistant.toolCall).toMatchObject({ call_id: "c-1", tool: "web.fetch" });
    expect(assistant.toolCall!.resolved).toBeUndefined();
  });

  it("resumeToolCall approve：续传增量回填并标记已裁决", async () => {
    useChatStore.setState({
      workspaceId: "ws-1",
      activeSessionId: "s-1",
      streaming: false,
      error: null,
      messages: [
        { key: "m-user", role: "user", content: "抓取" },
        {
          key: "m-asst",
          role: "assistant",
          content: "",
          toolCall: {
            call_id: "c-9",
            tool: "web.fetch",
            args: { url: "https://example.com" },
            risk: "external",
          },
        },
      ],
    });
    listSessionsMock.mockResolvedValue({ items: [], total: 0 });
    streamResumeMock.mockImplementation(async (params) => {
      expect(params.callId).toBe("c-9");
      expect(params.approve).toBe(true);
      params.handlers.onDelta("已抓取完成");
    });

    await useChatStore.getState().resumeToolCall(true);

    const state = useChatStore.getState();
    expect(state.streaming).toBe(false);
    expect(state.messages[1]!.content).toBe("已抓取完成");
    expect(state.messages[1]!.toolCall!.resolved).toBe("approved");
  });

  it("resumeToolCall deny：标记拒绝且不回填正文", async () => {
    useChatStore.setState({
      workspaceId: "ws-1",
      activeSessionId: "s-1",
      streaming: false,
      error: null,
      messages: [
        { key: "m-user", role: "user", content: "抓取" },
        {
          key: "m-asst",
          role: "assistant",
          content: "",
          toolCall: {
            call_id: "c-9",
            tool: "web.fetch",
            args: { url: "https://example.com" },
            risk: "external",
          },
        },
      ],
    });
    listSessionsMock.mockResolvedValue({ items: [], total: 0 });
    streamResumeMock.mockResolvedValue(undefined);

    await useChatStore.getState().resumeToolCall(false);

    const state = useChatStore.getState();
    expect(streamResumeMock.mock.calls[0]![0].approve).toBe(false);
    expect(state.messages[1]!.toolCall!.resolved).toBe("denied");
    expect(state.messages[1]!.content).toBe("");
  });

  it("resumeToolCall 请求层异常时解除挂起并提示错误", async () => {
    useChatStore.setState({
      workspaceId: "ws-1",
      activeSessionId: "s-1",
      streaming: false,
      error: null,
      messages: [
        {
          key: "m-asst",
          role: "assistant",
          content: "",
          toolCall: {
            call_id: "c-9",
            tool: "web.fetch",
            args: { url: "https://example.com" },
            risk: "external",
          },
        },
      ],
    });
    listSessionsMock.mockResolvedValue({ items: [], total: 0 });
    streamResumeMock.mockRejectedValue(new Error("聊天请求失败：该调用已处于 success 终态"));

    await useChatStore.getState().resumeToolCall(true);

    const state = useChatStore.getState();
    expect(state.error).toContain("已处于 success 终态");
    expect(state.messages[0]!.toolCall!.resolved).toBe("approved");
    expect(state.streaming).toBe(false);
  });

  it("clearError 清除错误提示", async () => {
    useChatStore.setState({ error: "旧错误" });
    useChatStore.getState().clearError();
    expect(useChatStore.getState().error).toBeNull();
  });
});
