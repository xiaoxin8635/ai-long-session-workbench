/**
 * workspace API（EchoDesk 前端）。
 *
 * 对接 memory-service `/api/workspaces`：当前用户的 workspace 列表。
 * 注册时自动创建个人 workspace，前端默认取第一个。
 */
import { api } from "./client";

/** workspace 信息。 */
export interface WorkspaceRead {
  id: string;
  name: string;
  owner_id: string;
  created_at: string;
}

/**
 * 列出当前用户可见的 workspace。
 *
 * @returns workspace 列表（个人部署恒为 1 个）。
 */
export async function listWorkspaces(): Promise<WorkspaceRead[]> {
  const { data } = await api.get<WorkspaceRead[]>("/api/workspaces");
  return data;
}
