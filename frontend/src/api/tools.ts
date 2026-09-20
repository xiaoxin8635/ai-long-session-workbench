/**
 * 工具 API（EchoDesk 前端，M-F3）。
 *
 * 对接 memory-service `/api/tools/*`：工具清单、直调执行（external 202 待确认）、
 * 确认/拒绝、调用审计。workspace_id 一律走 query 参数（CurrentWorkspaceQuery）。
 */
import { api } from "./client";

/** 工具风险分级（与服务端 ToolRiskLevel 对齐）。 */
export type ToolRisk = "read_only" | "write" | "external";

/** 调用状态（与服务端 ToolCallStatus 对齐）。 */
export type ToolCallStatus = "pending" | "success" | "denied" | "timeout" | "failed";

/** 工具清单条目（GET /api/tools）。 */
export interface ToolInfo {
  /** 工具名（如 web.fetch）。 */
  name: string;
  /** 描述。 */
  description: string;
  /** 风险分级。 */
  risk: ToolRisk;
  /** 参数 JSON Schema（MCP 同构）。 */
  args_schema: Record<string, unknown>;
}

/** 工具调用审计条目（GET /api/tools/calls）。 */
export interface ToolCall {
  /** 调用记录 ID。 */
  id: string;
  /** 来源会话（直调时可为 null）。 */
  session_id: string | null;
  /** 工具名。 */
  tool_name: string;
  /** 风险分级。 */
  risk_level: ToolRisk;
  /** 调用参数。 */
  args: Record<string, unknown>;
  /** 状态。 */
  status: ToolCallStatus;
  /** 确认人（external 调用裁决后回填）。 */
  approver_id: string | null;
  /** 结果摘要（2000 字符截断）。 */
  result_digest: string | null;
  /** 创建时间。 */
  created_at: string;
}

/** 执行工具响应体（202 pending / 200 终态）。 */
export interface ToolExecutionResult {
  /** 调用记录 ID。 */
  call_id: string;
  /** 当前状态。 */
  status: ToolCallStatus;
  /** 是否需要用户确认（external）。 */
  requires_confirmation: boolean;
  /** 执行结果（立即路径）。 */
  result: Record<string, unknown> | null;
  /** 失败原因（立即路径失败）。 */
  error: string | null;
}

/**
 * 列出全部已注册工具。
 *
 * @returns 工具清单（无 workspace 维度，全局注册表）。
 */
export async function listTools(): Promise<ToolInfo[]> {
  const { data } = await api.get<ToolInfo[]>("/api/tools");
  return data;
}

/**
 * 直调执行工具（external 风险返回 202 待确认）。
 *
 * @param workspaceId - workspace ID（query 参数）。
 * @param tool - 工具名。
 * @param args - 工具参数。
 * @param sessionId - 来源会话（审计挂链，可空）。
 * @returns 执行结果（requires_confirmation=true 时需走确认端点）。
 */
export async function executeTool(
  workspaceId: string,
  tool: string,
  args: Record<string, unknown>,
  sessionId?: string
): Promise<ToolExecutionResult> {
  const { data } = await api.post<ToolExecutionResult>(
    "/api/tools/execute",
    sessionId ? { tool, args, session_id: sessionId } : { tool, args },
    { params: { workspace_id: workspaceId } }
  );
  return data;
}

/**
 * 确认/拒绝一条 pending 的 external 调用（终态由后台任务落审计）。
 *
 * @param workspaceId - workspace ID（query 参数）。
 * @param callId - 调用记录 ID。
 * @param approve - true 执行 / false 拒绝。
 */
export async function confirmToolCall(
  workspaceId: string,
  callId: string,
  approve: boolean
): Promise<void> {
  await api.post(`/api/tools/calls/${callId}/confirm`, { approve }, {
    params: { workspace_id: workspaceId },
  });
}

/**
 * 拉取工具调用审计（created_at 倒序）。
 *
 * @param workspaceId - workspace ID（query 参数）。
 * @param sessionId - 会话过滤（可空）。
 * @param limit - 单页条数（服务端上限 100）。
 * @returns 审计清单。
 */
export async function listToolCalls(
  workspaceId: string,
  sessionId?: string,
  limit = 20
): Promise<ToolCall[]> {
  const { data } = await api.get<ToolCall[]>("/api/tools/calls", {
    params: { workspace_id: workspaceId, ...(sessionId ? { session_id: sessionId } : {}), limit },
  });
  return data;
}
