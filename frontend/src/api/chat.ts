/**
 * 聊天流式 API（EchoDesk 前端）。
 *
 * 对接 memory-service `POST /v1/chat/completions`（POST + SSE）。
 * 服务端事件契约（OpenAI chunk 扩展）：
 *   - 首片 delta.content 携带 metadata.session_id（自动建会话时前端由此取 ID）；
 *   - delta 片：choices[0].delta.content 为正文增量；
 *   - tool_call 片：metadata.tool_call 为 external 确认请求；
 *   - error：裸对象 {"error": {message, type}}，随后 [DONE]；
 *   - finish 片：finish_reason=stop，metadata 含 citations / pending_confirmation。
 */
import { consumeSse } from "../lib/sse";
import { useAuthStore } from "../stores/auth";

/** RAG 引用项（finish 片 metadata.citations）。 */
export interface Citation {
  chunk_id: string;
  file_id: string;
  filename: string;
  chunk_index: number;
  score: number;
}

/** 流式回调集合。 */
export interface ChatStreamHandlers {
  /** 捕获服务端会话 ID（首片）。 */
  onSessionId: (sessionId: string) => void;
  /** 正文增量。 */
  onDelta: (text: string) => void;
  /** external 工具确认请求（M-F3 消费）。 */
  onToolCall: (toolCall: Record<string, unknown>) => void;
  /** 服务端错误事件。 */
  onError: (message: string, type: string) => void;
  /** 正常收敛（finish 片；含 citations 与挂起确认）。 */
  onFinish: (info: { citations: Citation[]; pendingConfirmation: unknown }) => void;
}

/** streamChat 请求参数。 */
export interface ChatStreamParams {
  /** workspace ID（metadata 必填）。 */
  workspaceId: string;
  /** 会话 ID；缺省时服务端自动创建（经 onSessionId 回传）。 */
  sessionId?: string;
  /** 本轮用户消息文本。 */
  content: string;
  /** 是否启用 Agent 工具循环（默认 true）。 */
  enableTools?: boolean;
  /** 流式回调集合。 */
  handlers: ChatStreamHandlers;
}

/**
 * 发起流式对话并消费全部 SSE 事件。
 *
 * @param params - 请求参数与回调。
 * @returns 正常结束（收到 [DONE]）时 resolve；网络/HTTP 层失败时 reject。
 */
export async function streamChat(params: ChatStreamParams): Promise<void> {
  const token = useAuthStore.getState().accessToken;
  if (!token) {
    throw new Error("未登录");
  }
  const body = {
    messages: [{ role: "user", content: params.content }],
    stream: true,
    metadata: {
      workspace_id: params.workspaceId,
      session_id: params.sessionId,
      enable_tools: params.enableTools ?? true,
    },
  };
  const resp = await fetch("/v1/chat/completions", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  if (!resp.ok || resp.body === null) {
    throw new Error(`聊天请求失败：HTTP ${resp.status}`);
  }

  let citations: Citation[] = [];
  let pendingConfirmation: unknown = null;

  await consumeSse(resp.body, (event) => {
    if (event.data === "[DONE]") {
      return;
    }
    let payload: unknown;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return; // 非 JSON 行忽略
    }
    if (payload === null || typeof payload !== "object") {
      return;
    }
    const chunk = payload as Record<string, unknown>;

    // 裸 error 事件（非 chunk 结构）
    if ("error" in chunk && chunk.error !== null && typeof chunk.error === "object") {
      const err = chunk.error as { message?: string; type?: string };
      params.handlers.onError(err.message ?? "未知错误", err.type ?? "internal_error");
      return;
    }

    const metadata = (chunk.metadata ?? {}) as Record<string, unknown>;
    if (typeof metadata.session_id === "string") {
      params.handlers.onSessionId(metadata.session_id);
    }
    if (metadata.tool_call !== undefined && metadata.tool_call !== null) {
      params.handlers.onToolCall(metadata.tool_call as Record<string, unknown>);
    }

    const choices = chunk.choices as Array<Record<string, unknown>> | undefined;
    const choice = choices?.[0];
    const delta = choice?.delta as { content?: string } | undefined;
    if (typeof delta?.content === "string" && delta.content.length > 0) {
      params.handlers.onDelta(delta.content);
    }
    if (choice?.finish_reason === "stop") {
      if (Array.isArray(metadata.citations)) {
        citations = metadata.citations as Citation[];
      }
      pendingConfirmation = metadata.pending_confirmation ?? null;
    }
  });

  params.handlers.onFinish({ citations, pendingConfirmation });
}
