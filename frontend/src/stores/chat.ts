/**
 * 聊天状态 store（EchoDesk 前端）。
 *
 * 职责：workspace/会话列表管理、当前会话消息、流式发送状态机
 * （乐观追加 user 消息 + assistant 占位 → SSE 增量回填 → citations/错误落地）。
 */
import { create } from "zustand";
import * as chatApi from "../api/chat";
import type { Citation, ToolCallRequest } from "../api/chat";
import * as sessionsApi from "../api/sessions";
import type { MessageRead, SessionRead } from "../api/sessions";
import { listWorkspaces } from "../api/workspaces";
import { errorMessage } from "./auth";

/** 聊天界面消息（比 API MessageRead 多流式期间的前端态字段）。 */
export interface ChatMessage {
  /** 前端唯一键（历史消息用服务端 ID，流式消息用 `local-<n>`）。 */
  key: string;
  role: "user" | "assistant";
  content: string;
  /** 助手消息的 RAG 引用（finish 片落地）。 */
  citations?: Citation[];
  /** 流式失败标记（展示重试提示）。 */
  failed?: boolean;
  /** external 工具确认请求（挂在本条助手消息上；resolved 记录裁决结果）。 */
  toolCall?: ToolCallRequest & { resolved?: "approved" | "denied" };
}

/** 聊天 store 状态与动作。 */
interface ChatState {
  /** 当前 workspace（bootstrap 后填充）。 */
  workspaceId: string | null;
  /** 会话列表（updated_at 降序）。 */
  sessions: SessionRead[];
  /** 当前选中会话（null = 未发送首条消息的草稿态）。 */
  activeSessionId: string | null;
  /** 当前会话消息。 */
  messages: ChatMessage[];
  /** 流式接收中（禁输入、显示打字点）。 */
  streaming: boolean;
  /** 页面级错误提示。 */
  error: string | null;
  /** 清除页面级错误提示（错误条关闭按钮）。 */
  clearError: () => void;

  /** 初始化：拉取 workspace 与会话列表（登录后/刷新后调用）。 */
  bootstrap: () => Promise<void>;
  /** 选中会话并加载历史（最近一页）。 */
  selectSession: (sessionId: string) => Promise<void>;
  /** 进入草稿态（新建对话：清空当前选择与消息）。 */
  startDraft: () => void;
  /** 发送消息（流式）。 */
  sendMessage: (content: string) => Promise<void>;
  /** 停止当前流式生成（中断 SSE，保留已到达的部分正文）。 */
  stopStreaming: () => void;
  /** 重新生成最后一条助手回答（丢弃旧答复，重跑同一轮用户消息）。 */
  regenerate: () => Promise<void>;
  /** 裁决当前挂起的 external 工具调用并续传回答（SSE）。 */
  resumeToolCall: (approve: boolean) => Promise<void>;
  /** 重命名会话。 */
  renameSession: (sessionId: string, title: string) => Promise<void>;
  /** 删除会话（若删的是当前会话则回到草稿态）。 */
  removeSession: (sessionId: string) => Promise<void>;
}

/** 前端本地消息自增键。 */
let localKeySeq = 0;

/** 当前流式请求的中断控制器（「停止生成」用；无进行中为 null）。 */
let activeController: AbortController | null = null;

/** 判定是否为用户主动中断（fetch abort 抛 DOMException AbortError）。 */
function isAbortError(err: unknown): boolean {
  return err instanceof DOMException && err.name === "AbortError";
}

export const useChatStore = create<ChatState>()((set, get) => ({
  workspaceId: null,
  sessions: [],
  activeSessionId: null,
  messages: [],
  streaming: false,
  error: null,

  bootstrap: async () => {
    try {
      const workspaces = await listWorkspaces();
      const wsId = workspaces[0]?.id ?? null;
      set({ workspaceId: wsId });
      if (wsId) {
        await refreshSessions();
      }
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  clearError: () => {
    set({ error: null });
  },

  selectSession: async (sessionId) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return;
    }
    set({ activeSessionId: sessionId, messages: [], error: null });
    try {
      const page = await sessionsApi.listMessages(wsId, sessionId);
      const offset = Math.max(0, page.total - 100);
      const recent =
        offset === 0
          ? page
          : await sessionsApi.listMessages(wsId, sessionId, 100, offset);
      set({
        messages: recent.items
          .filter((m): m is MessageRead & { role: "user" | "assistant" } =>
            m.role === "user" || m.role === "assistant"
          )
          .map((m) => ({ key: m.id, role: m.role, content: m.content })),
      });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  startDraft: () => {
    set({ activeSessionId: null, messages: [], error: null });
  },

  sendMessage: async (content) => {
    await runTurn(content, true);
  },

  stopStreaming: () => {
    if (activeController !== null) {
      activeController.abort();
    }
  },

  regenerate: async () => {
    const { messages, streaming } = get();
    if (streaming) {
      return;
    }
    // 找最后一条用户消息，以其为错重跑（runTurn 内部会截断其后的旧助手回复）
    let lastUserContent: string | null = null;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      if (messages[i]!.role === "user") {
        lastUserContent = messages[i]!.content;
        break;
      }
    }
    if (lastUserContent === null) {
      return;
    }
    await runTurn(lastUserContent, false);
  },

  resumeToolCall: async (approve) => {
    const { workspaceId, activeSessionId, messages, streaming } = get();
    if (!workspaceId || !activeSessionId || streaming) {
      return;
    }
    const idx = messages.findIndex((m) => m.toolCall !== undefined && m.toolCall.resolved === undefined);
    if (idx === -1) {
      return;
    }
    const targetKey = messages[idx]!.key;
    const callId = messages[idx]!.toolCall!.call_id;
    set({ streaming: true, error: null });

    // 就地向携带 toolCall 的助手消息追加续传增量 / 回写裁决结果
    const patchTarget = (patch: (m: ChatMessage) => ChatMessage): void => {
      const msgs = get().messages;
      const i = msgs.findIndex((m) => m.key === targetKey);
      if (i !== -1) {
        const updated = [...msgs];
        updated[i] = patch(updated[i]!);
        set({ messages: updated });
      }
    };

    try {
      await chatApi.streamResume({
        workspaceId,
        sessionId: activeSessionId,
        callId,
        approve,
        handlers: {
          onSessionId: () => undefined, // resume 不换会话
          onDelta: (text) => {
            patchTarget((m) => ({ ...m, content: m.content + text }));
          },
          onToolCall: () => undefined,
          onError: (message) => {
            set({ error: message });
          },
          onFinish: () => undefined,
        },
      });
      patchTarget((m) => ({ ...m, toolCall: { ...m.toolCall!, resolved: approve ? "approved" : "denied" } }));
      await refreshSessions();
    } catch (err) {
      set({ error: errorMessage(err) });
      // HTTP 层失败（如 409 已终态）：解除挂起避免确认卡片永久卡住
      patchTarget((m) => ({ ...m, toolCall: { ...m.toolCall!, resolved: approve ? "approved" : "denied" } }));
    } finally {
      set({ streaming: false });
    }
  },

  renameSession: async (sessionId, title) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return;
    }
    try {
      await sessionsApi.updateSession(wsId, sessionId, { title });
      await refreshSessions();
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  removeSession: async (sessionId) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return;
    }
    try {
      await sessionsApi.deleteSession(wsId, sessionId);
      if (get().activeSessionId === sessionId) {
        get().startDraft();
      }
      await refreshSessions();
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },
}));

/**
 * 刷新会话列表（内部助手，避免各动作重复样板）。
 */
async function refreshSessions(): Promise<void> {
  const wsId = useChatStore.getState().workspaceId;
  if (!wsId) {
    return;
  }
  const page = await sessionsApi.listSessions(wsId);
  useChatStore.setState({ sessions: page.items });
}

/**
 * 流式一轮回答的共享内核（sendMessage 追加用户消息 / regenerate 重跑共用）。
 *
 * @param content - 本轮用户消息文本。
 * @param appendUser - true 追加新的用户气泡（首次发送）；false 仅截断到最后一条
 *   用户消息后重跑助手回答（重新生成）。
 */
async function runTurn(content: string, appendUser: boolean): Promise<void> {
  const get = useChatStore.getState;
  const set = useChatStore.setState;
  const { workspaceId, activeSessionId, streaming } = get();
  if (workspaceId === null || streaming) {
    return;
  }
  if (appendUser && content.trim() === "") {
    return;
  }

  let baseMessages = get().messages;
  if (!appendUser) {
    // 重新生成：截断到最后一条用户消息（含），丢弃其后旧助手回复
    let lastUser = -1;
    for (let i = baseMessages.length - 1; i >= 0; i -= 1) {
      if (baseMessages[i]!.role === "user") {
        lastUser = i;
        break;
      }
    }
    if (lastUser === -1) {
      return;
    }
    baseMessages = baseMessages.slice(0, lastUser + 1);
  }

  const nextMessages: ChatMessage[] = appendUser
    ? [
        ...baseMessages,
        { key: `local-${(localKeySeq += 1)}`, role: "user", content },
        { key: `local-${(localKeySeq += 1)}`, role: "assistant", content: "" },
      ]
    : [...baseMessages, { key: `local-${(localKeySeq += 1)}`, role: "assistant", content: "" }];
  const assistantKey = nextMessages[nextMessages.length - 1]!.key;
  set({ streaming: true, error: null, messages: nextMessages });

  // 就地更新流式中的 assistant 占位
  const patchAssistant = (patch: (m: ChatMessage) => ChatMessage): void => {
    const msgs = get().messages;
    const idx = msgs.findIndex((m) => m.key === assistantKey);
    if (idx !== -1) {
      const updated = [...msgs];
      updated[idx] = patch(updated[idx]!);
      set({ messages: updated });
    }
  };
  const appendDelta = (text: string): void => {
    patchAssistant((m) => ({ ...m, content: m.content + text }));
  };
  const markFailed = (): void => {
    patchAssistant((m) => ({ ...m, failed: true }));
  };

  const controller = new AbortController();
  activeController = controller;
  try {
    await chatApi.streamChat({
      workspaceId,
      sessionId: activeSessionId ?? undefined,
      content,
      signal: controller.signal,
      handlers: {
        onSessionId: (sessionId) => {
          if (get().activeSessionId !== sessionId) {
            set({ activeSessionId: sessionId });
            void refreshSessions();
          }
        },
        onDelta: appendDelta,
        onToolCall: (toolCall) => {
          patchAssistant((m) => ({ ...m, toolCall }));
        },
        onError: (message) => {
          set({ error: message });
          markFailed();
        },
        onFinish: ({ citations }) => {
          if (citations.length === 0) {
            return;
          }
          patchAssistant((m) => ({ ...m, citations }));
        },
      },
    });
    // 流结束后刷新会话列表（token_total/last_message_at/摘要标题更新）
    await refreshSessions();
  } catch (err) {
    if (!isAbortError(err)) {
      set({ error: errorMessage(err) });
      markFailed();
    }
    // 用户主动停止：保留已到达的部分正文，不标记失败、不弹错误
  } finally {
    activeController = null;
    set({ streaming: false });
  }
}
