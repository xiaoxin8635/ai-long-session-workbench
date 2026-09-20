/**
 * 记忆管理 API（EchoDesk 前端，M-F4）。
 *
 * 对接 memory-service `/api/memories/*`：列表（过滤/分页，conflicted 置顶）、
 * 详情（版本链+事件流水）、用户编辑、软删除、冲突裁决、检索测试。
 * workspace_id 一律走 query 参数（CurrentWorkspaceQuery）。
 */
import { api } from "./client";

/** 记忆类型（服务端 MemoryType）。 */
export type MemoryType = "episodic" | "semantic" | "procedural";

/** 记忆状态（服务端 MemoryStatus）。 */
export type MemoryStatus =
  | "active"
  | "superseded"
  | "conflicted"
  | "archived"
  | "deleted";

/** 记忆条目视图。 */
export interface MemoryItem {
  id: string;
  memory_type: MemoryType;
  key: string;
  content: string;
  confidence: number;
  importance: number;
  status: MemoryStatus;
  version: number;
  source_session_id: string | null;
  source_message_ids: string[];
  supersedes_id: string | null;
  expires_at: string | null;
  hit_count: number;
  last_hit_at: string | null;
  created_at: string;
  updated_at: string;
}

/** 记忆事件流水条目。 */
export interface MemoryEvent {
  event_type: "create" | "update" | "merge" | "conflict" | "supersede" | "hit";
  old_value: string | null;
  new_value: string | null;
  source: "llm" | "user" | "system";
  created_at: string;
}

/** 记忆详情：基础字段 + 版本链 + 事件流水。 */
export interface MemoryDetail extends MemoryItem {
  version_chain: MemoryItem[];
  events: MemoryEvent[];
}

/** 检索命中条目（含综合分与相似度）。 */
export interface MemorySearchHit {
  id: string;
  memory_type: MemoryType;
  key: string;
  content: string;
  confidence: number;
  importance: number;
  score: number;
  similarity: number;
}

/** 分页容器。 */
export interface Page<T> {
  items: T[];
  total: number;
}

/** 列表过滤条件。 */
export interface MemoryFilters {
  memory_type?: MemoryType;
  status?: MemoryStatus;
  q?: string;
  limit?: number;
  offset?: number;
}

/**
 * 记忆列表（conflicted 置顶 + updated_at 倒序）。
 *
 * @param workspaceId - workspace ID（query 参数）。
 * @param filters - 类型/状态/关键词/分页过滤。
 * @returns 记忆分页。
 */
export async function listMemories(
  workspaceId: string,
  filters: MemoryFilters = {}
): Promise<Page<MemoryItem>> {
  const { data } = await api.get<Page<MemoryItem>>("/api/memories", {
    params: {
      workspace_id: workspaceId,
      ...(filters.memory_type ? { memory_type: filters.memory_type } : {}),
      ...(filters.status ? { status: filters.status } : {}),
      ...(filters.q ? { q: filters.q } : {}),
      limit: filters.limit ?? 20,
      offset: filters.offset ?? 0,
    },
  });
  return data;
}

/**
 * 记忆详情（版本链 + 最近 20 条事件流水）。
 *
 * @param workspaceId - workspace ID。
 * @param memoryId - 记忆 ID。
 * @returns 详情。
 */
export async function getMemory(workspaceId: string, memoryId: string): Promise<MemoryDetail> {
  const { data } = await api.get<MemoryDetail>(`/api/memories/${memoryId}`, {
    params: { workspace_id: workspaceId },
  });
  return data;
}

/**
 * 用户编辑记忆（version+1，edit_by_user 审计）。
 *
 * @param workspaceId - workspace ID。
 * @param memoryId - 记忆 ID。
 * @param patch - 待更新字段（content/confidence/importance/expires_at）。
 * @returns 更新后的条目。
 */
export async function updateMemory(
  workspaceId: string,
  memoryId: string,
  patch: { content?: string; confidence?: number; importance?: number; expires_at?: string | null }
): Promise<MemoryItem> {
  const { data } = await api.patch<MemoryItem>(`/api/memories/${memoryId}`, patch, {
    params: { workspace_id: workspaceId },
  });
  return data;
}

/**
 * 软删除记忆（列表与检索不再可见，事件留痕）。
 *
 * @param workspaceId - workspace ID。
 * @param memoryId - 记忆 ID。
 */
export async function deleteMemory(workspaceId: string, memoryId: string): Promise<void> {
  await api.delete(`/api/memories/${memoryId}`, { params: { workspace_id: workspaceId } });
}

/**
 * 冲突裁决：保留本条（this）或同 key 的对手方（other）。
 *
 * @param workspaceId - workspace ID。
 * @param memoryId - 当前查看（conflicted）条目 ID。
 * @param keep - 保留方向。
 * @returns 胜出条目。
 */
export async function resolveMemory(
  workspaceId: string,
  memoryId: string,
  keep: "this" | "other"
): Promise<MemoryItem> {
  const { data } = await api.post<MemoryItem>(
    `/api/memories/${memoryId}/resolve`,
    { keep },
    { params: { workspace_id: workspaceId } }
  );
  return data;
}

/**
 * 检索测试（与对话链路同检索器）。
 *
 * @param workspaceId - workspace ID（query 参数）。
 * @param query - 模拟查询。
 * @param topK - 召回条数（1-50）。
 * @returns 命中列表（score 降序）。
 */
export async function searchMemories(
  workspaceId: string,
  query: string,
  topK = 10
): Promise<MemorySearchHit[]> {
  const { data } = await api.post<MemorySearchHit[]>(
    "/api/memories/search",
    { query, top_k: topK },
    { params: { workspace_id: workspaceId } }
  );
  return data;
}
