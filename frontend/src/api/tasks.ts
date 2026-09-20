/**
 * 任务/待办 API（EchoDesk 前端，M-F5）。
 *
 * 对接 memory-service `/api/tasks`（M-10）：与 Agent 的 todo 工具共用
 * 同一 TaskService，状态变更自动同步 job.*.progress 记忆。
 * workspace_id 走 query 参数；创建/更新可选挂 source 会话。
 */
import { api } from "./client";

/** 任务状态（服务端 TaskStatus）。 */
export type TaskStatus = "open" | "doing" | "done" | "abandoned";

/** 任务条目。 */
export interface Task {
  id: string;
  title: string;
  status: TaskStatus;
  /** 0 普通 / 1 高（开场注入简报）。 */
  priority: number;
  due_date: string | null;
  related_session_ids: string[];
  created_at: string;
  updated_at: string;
}

/** 创建任务请求体。 */
export interface TaskCreatePayload {
  title: string;
  priority?: number;
  due_date?: string | null;
}

/** 更新任务请求体（PATCH 语义：仅提交字段生效）。 */
export interface TaskUpdatePayload {
  title?: string;
  status?: TaskStatus;
  priority?: number;
  due_date?: string | null;
}

/**
 * 任务列表（最近变动在前）。
 *
 * @param workspaceId - workspace ID（query 参数）。
 * @param status - 状态过滤；缺省全部。
 * @returns 任务列表。
 */
export async function listTasks(
  workspaceId: string,
  status?: TaskStatus
): Promise<Task[]> {
  const { data } = await api.get<Task[]>("/api/tasks", {
    params: { workspace_id: workspaceId, ...(status ? { status } : {}) },
  });
  return data;
}

/**
 * 创建任务（并同步 job.*.progress 记忆）。
 *
 * @param workspaceId - workspace ID。
 * @param payload - 标题/优先级/截止时间。
 * @returns 新建任务。
 */
export async function createTask(
  workspaceId: string,
  payload: TaskCreatePayload
): Promise<Task> {
  const { data } = await api.post<Task>("/api/tasks", payload, {
    params: { workspace_id: workspaceId },
  });
  return data;
}

/**
 * 更新任务（PATCH 语义；状态/标题变更触发记忆同步）。
 *
 * @param workspaceId - workspace ID。
 * @param taskId - 任务 ID。
 * @param patch - 待更新字段。
 * @returns 更新后的任务。
 */
export async function updateTask(
  workspaceId: string,
  taskId: string,
  patch: TaskUpdatePayload
): Promise<Task> {
  const { data } = await api.patch<Task>(`/api/tasks/${taskId}`, patch, {
    params: { workspace_id: workspaceId },
  });
  return data;
}
