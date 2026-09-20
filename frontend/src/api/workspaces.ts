/**
 * workspace API（EchoDesk 前端）。
 *
 * 对接 memory-service `/api/workspaces`：当前用户的 workspace 列表、
 * 详情与成员管理（ADMIN+）。
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

/** 成员角色（服务端 MemberRole）。 */
export type MemberRole = "owner" | "admin" | "member";

/** workspace 成员。 */
export interface WorkspaceMember {
  workspace_id: string;
  user_id: string;
  username: string;
  display_name: string | null;
  role: MemberRole;
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

/**
 * 列出 workspace 成员（ADMIN+，403 防枚举）。
 *
 * @param workspaceId - workspace ID。
 * @returns 成员列表。
 */
export async function listMembers(workspaceId: string): Promise<WorkspaceMember[]> {
  const { data } = await api.get<WorkspaceMember[]>(
    `/api/workspaces/${workspaceId}/members`
  );
  return data;
}

/**
 * 按用户名添加成员；已存在则更新角色（ADMIN+）。
 *
 * @param workspaceId - workspace ID。
 * @param username - 目标用户名。
 * @param role - 授予角色（owner 禁止经此接口授予）。
 * @returns 成员信息。
 */
export async function addMember(
  workspaceId: string,
  username: string,
  role: Exclude<MemberRole, "owner">
): Promise<WorkspaceMember> {
  const { data } = await api.post<WorkspaceMember>(
    `/api/workspaces/${workspaceId}/members`,
    { username, role }
  );
  return data;
}
