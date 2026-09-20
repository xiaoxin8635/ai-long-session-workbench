/**
 * 聊天流式 API（EchoDesk 前端）。
 *
 * 对接 memory-service `POST /v1/chat/completions` 与 `POST /v1/chat/resume`
 * （均为 POST + SSE）。服务端事件契约（OpenAI chunk 扩展）：
 *   - 首片 delta.content 携带 metadata.session_id（自动建会话时前端由此取 ID）；
 *   - delta 片：choices[0].delta.content 为正文增量；
 *   - tool_call 片：metadata.tool_call 为 external 确认请求 {call_id, tool, args, risk}；
 *   - error：裸对象 {"error": {message, type}}，随后 [DONE]；
 *   - finish 片：finish_reason=stop，metadata 含 citations / pending_confirmation。
 * resume 路径同构（无 citations），恢复被 external 工具挂起的对话。
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

/** external 工具确认请求（tool_call 片 metadata.tool_call）。 */
export interface ToolCallRequest {
  /** tool_call_logs 记录 ID（resume 裁决对象）。 */
  call_id: string;
  /** 工具名（如 web.fetch）。 */
  tool: string;
  /** 工具参数。 */
  args: Record<string, unknown>;
  /** 风险分级（read_only / write / external）。 */
  risk: string;
}

/** 流式回调集合。 */
export interface ChatStreamHandlers {
  /** 捕获服务端会话 ID（首片）。 */
  onSessionId: (sessionId: string) => void;
  /** 正文增量。 */
  onDelta: (text: string) => void;
  /** external 工具确认请求。 */
  onToolCall: (toolCall: ToolCallRequest) => void;
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

/** streamResume 请求参数（external 工具裁决后续传）。 */
export interface ChatResumeParams {
  /** workspace ID。 */
  workspaceId: string;
  /** 挂起所在会话 ID。 */
  sessionId: string;
  /** 待裁决的调用 ID。 */
  callId: string;
  /** true 执行工具 / false 拒绝。 */
  approve: boolean;
  /** 流式回调集合（与主对话同构）。 */
  handlers: ChatStreamHandlers;
}

/**
 * 打开 SSE 流（登录校验 + Bearer + HTTP 层错误归一）。
 *
 * @param url - 请求路径。
 * @param body - JSON 请求体。
 * @returns 响应字节流。
 */
async function openStream(url: string, body: unknown): Promise<ReadableStream<Uint8Array>> {
  const token = useAuthStore.getState().accessToken;
  if (!token) {
    throw new Error("未登录");
  }
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  if (!resp.ok || resp.body === null) {
    // 尽力解析 RFC 7807 problem+json 的中文 detail（如 resume 409 已终态）
    let detail = `HTTP ${resp.status}`;
    try {
      const problem = (await resp.json()) as { detail?: unknown };
      if (typeof problem?.detail === "string" && problem.detail !== "") {
        detail = problem.detail;
      }
    } catch {
      // 非 JSON 响应体，保留 HTTP 状态码文案
    }
    throw new Error(`聊天请求失败：${detail}`);
  }
  return resp.body;
}

/**
 * 消费聊天 SSE 字节流：解析分片并驱动回调，结束时触发 onFinish。
 *
 * @param body - 响应字节流。
 * @param handlers - 流式回调集合。
 */
async function consumeChatEvents(
  body: ReadableStream<Uint8Array>,
  handlers: ChatStreamHandlers
): Promise<void> {
  let citations: Citation[] = [];
  let pendingConfirmation: unknown = null;

  await consumeSse(body, (event) => {
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
      handlers.onError(err.message ?? "未知错误", err.type ?? "internal_error");
      return;
    }

    const metadata = (chunk.metadata ?? {}) as Record<string, unknown>;
    if (typeof metadata.session_id === "string") {
      handlers.onSessionId(metadata.session_id);
    }
    if (metadata.tool_call !== undefined && metadata.tool_call !== null) {
      handlers.onToolCall(metadata.tool_call as ToolCallRequest);
    }

    const choices = chunk.choices as Array<Record<string, unknown>> | undefined;
    const choice = choices?.[0];
    const delta = choice?.delta as { content?: string } | undefined;
    if (typeof delta?.content === "string" && delta.content.length > 0) {
      handlers.onDelta(delta.content);
    }
    if (choice?.finish_reason === "stop") {
      if (Array.isArray(metadata.citations)) {
        citations = metadata.citations as Citation[];
      }
      pendingConfirmation = metadata.pending_confirmation ?? null;
    }
  });

  handlers.onFinish({ citations, pendingConfirmation });
}

/**
 * 发起流式对话并消费全部 SSE 事件。
 *
 * @param params - 请求参数与回调。
 * @returns 正常结束（收到 [DONE]）时 resolve；网络/HTTP 层失败时 reject。
 */
export async function streamChat(params: ChatStreamParams): Promise<void> {
  const body = {
    messages: [{ role: "user", content: params.content }],
    stream: true,
    metadata: {
      workspace_id: params.workspaceId,
      session_id: params.sessionId,
      enable_tools: params.enableTools ?? true,
    },
  };
  const respBody = await openStream("/v1/chat/completions", body);
  await consumeChatEvents(respBody, params.handlers);
}

/**
 * 裁决 external 工具调用并续传剩余回答（SSE）。
 *
 * @param params - 裁决参数与回调。
 * @returns 正常结束时 resolve；HTTP 层失败（如 409 已终态）时 reject。
 */
export async function streamResume(params: ChatResumeParams): Promise<void> {
  const body = {
    workspace_id: params.workspaceId,
    session_id: params.sessionId,
    call_id: params.callId,
    approve: params.approve,
  };
  const respBody = await openStream("/v1/chat/resume", body);
  await consumeChatEvents(respBody, params.handlers);
}
