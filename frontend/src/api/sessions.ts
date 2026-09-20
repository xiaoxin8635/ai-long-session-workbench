/**
 * 会话 API（EchoDesk 前端）。
 *
 * 对接 memory-service `/api/sessions/*`：CRUD + 消息历史分页。
 * 注意：该路由族 workspace_id 一律走 query 参数（服务端 CurrentWorkspaceQuery），
 * body 只承载业务字段。
 */
import { api } from "./client";

/** 会话信息。 */
export interface SessionRead {
  id: string;
  workspace_id: string;
  user_id: string;
  title: string;
  status: string;
  token_total: number;
  last_message_at: string | null;
  version: number;
  created_at: string;
  updated_at: string;
}

/** 会话消息。 */
export interface MessageRead {
  id: string;
  session_id: string;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  token_count: number;
  version: number;
  metadata: Record<string, unknown>;
  created_at: string;
}

/** 分页容器。 */
export interface Page<T> {
  items: T[];
  total: number;
}

/**
 * 列出当前 workspace 的会话（updated_at 降序）。
 *
 * @param workspaceId - workspace ID。
 * @param limit - 单页条数（服务端上限 100）。
 * @returns 会话分页。
 */
export async function listSessions(workspaceId: string, limit = 50): Promise<Page<SessionRead>> {
  const { data } = await api.get<Page<SessionRead>>("/api/sessions", {
    params: { workspace_id: workspaceId, limit },
  });
  return data;
}

/**
 * 创建会话。
 *
 * @param workspaceId - workspace ID。
 * @param title - 标题（可空，服务端用占位、首条消息后摘要命名）。
 * @returns 新会话。
 */
export async function createSession(workspaceId: string, title?: string): Promise<SessionRead> {
  const { data } = await api.post<SessionRead>("/api/sessions", title === undefined ? {} : { title }, {
    params: { workspace_id: workspaceId },
  });
  return data;
}

/**
 * 更新会话（重命名 / 归档）。
 *
 * @param workspaceId - workspace ID。
 * @param sessionId - 会话 ID。
 * @param patch - 待更新字段。
 * @returns 更新后的会话。
 */
export async function updateSession(
  workspaceId: string,
  sessionId: string,
  patch: { title?: string; status?: string }
): Promise<SessionRead> {
  const { data } = await api.patch<SessionRead>(`/api/sessions/${sessionId}`, patch, {
    params: { workspace_id: workspaceId },
  });
  return data;
}

/**
 * 删除会话（软删除）。
 *
 * @param workspaceId - workspace ID。
 * @param sessionId - 会话 ID。
 */
export async function deleteSession(workspaceId: string, sessionId: string): Promise<void> {
  await api.delete(`/api/sessions/${sessionId}`, { params: { workspace_id: workspaceId } });
}

/**
 * 拉取会话消息历史（时间正序）。
 *
 * @param workspaceId - workspace ID。
 * @param sessionId - 会话 ID。
 * @param limit - 单页条数（服务端上限 100）。
 * @param offset - 偏移量（长会话取最近一页：offset = max(0, total - limit)）。
 * @returns 消息分页。
 */
export async function listMessages(
  workspaceId: string,
  sessionId: string,
  limit = 100,
  offset = 0
): Promise<Page<MessageRead>> {
  const { data } = await api.get<Page<MessageRead>>(`/api/sessions/${sessionId}/messages`, {
    params: { workspace_id: workspaceId, limit, offset },
  });
  return data;
}
