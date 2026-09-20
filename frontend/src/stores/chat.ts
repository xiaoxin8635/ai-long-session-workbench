/**
 * 聊天状态 store（EchoDesk 前端）。
 *
 * 职责：workspace/会话列表管理、当前会话消息、流式发送状态机
 * （乐观追加 user 消息 + assistant 占位 → SSE 增量回填 → citations/错误落地）。
 */
import { create } from "zustand";
import * as chatApi from "../api/chat";
import type { Citation } from "../api/chat";
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
  /** 重命名会话。 */
  renameSession: (sessionId: string, title: string) => Promise<void>;
  /** 删除会话（若删的是当前会话则回到草稿态）。 */
  removeSession: (sessionId: string) => Promise<void>;
}

/** 前端本地消息自增键。 */
let localKeySeq = 0;

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
    const { workspaceId, activeSessionId, streaming } = get();
    if (!workspaceId || streaming || content.trim() === "") {
      return;
    }
    const userMsg: ChatMessage = { key: `local-${(localKeySeq += 1)}`, role: "user", content };
    const assistantKey = `local-${(localKeySeq += 1)}`;
    const assistantMsg: ChatMessage = { key: assistantKey, role: "assistant", content: "" };
    set({ streaming: true, error: null, messages: [...get().messages, userMsg, assistantMsg] });

    // 就地更新流式中的 assistant 占位
    const appendDelta = (text: string): void => {
      const msgs = get().messages;
      const idx = msgs.findIndex((m) => m.key === assistantKey);
      if (idx === -1) {
        return;
      }
      const updated = [...msgs];
      updated[idx] = { ...updated[idx], content: updated[idx].content + text };
      set({ messages: updated });
    };
    const markFailed = (): void => {
      const msgs = get().messages;
      const idx = msgs.findIndex((m) => m.key === assistantKey);
      if (idx !== -1) {
        const updated = [...msgs];
        updated[idx] = { ...updated[idx], failed: true };
        set({ messages: updated });
      }
    };

    try {
      await chatApi.streamChat({
        workspaceId,
        sessionId: activeSessionId ?? undefined,
        content,
        handlers: {
          onSessionId: (sessionId) => {
            if (get().activeSessionId !== sessionId) {
              set({ activeSessionId: sessionId });
              void refreshSessions();
            }
          },
          onDelta: appendDelta,
          onToolCall: () => undefined, // M-F3 接确认卡片
          onError: (message) => {
            set({ error: message });
            markFailed();
          },
          onFinish: ({ citations }) => {
            if (citations.length === 0) {
              return;
            }
            const msgs = get().messages;
            const idx = msgs.findIndex((m) => m.key === assistantKey);
            if (idx !== -1) {
              const updated = [...msgs];
              updated[idx] = { ...updated[idx], citations };
              set({ messages: updated });
            }
          },
        },
      });
      // 流结束后刷新会话列表（token_total/last_message_at/摘要标题更新）
      await refreshSessions();
    } catch (err) {
      set({ error: errorMessage(err) });
      markFailed();
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
