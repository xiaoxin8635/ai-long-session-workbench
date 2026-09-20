/**
 * 用量统计 API（EchoDesk 前端，M-F5）。
 *
 * 对接 memory-service `GET /api/usage/summary`（M-11）：窗口内 token 总量、
 * 区块拆分（memory/rag/tool）、会话数、轮次数与按日趋势。
 * workspace_id 走 query 参数（CurrentWorkspaceQuery）。
 */
import { api } from "./client";

/** 窗口内 token 总量与区块拆分。 */
export interface UsageTotals {
  prompt_tokens: number;
  completion_tokens: number;
  memory_tokens: number;
  rag_tokens: number;
  tool_tokens: number;
}

/** 按日聚合项。 */
export interface UsageDayItem {
  day: string;
  prompt_tokens: number;
  completion_tokens: number;
}

/** 用量汇总响应。 */
export interface UsageSummary {
  workspace_id: string;
  days: number;
  totals: UsageTotals;
  /** 窗口内有用量的会话数。 */
  sessions: number;
  /** 窗口内对话轮次数。 */
  turns: number;
  by_day: UsageDayItem[];
}

/**
 * token 用量聚合。
 *
 * @param workspaceId - workspace ID。
 * @param days - 统计窗口天数（1-90，缺省 7）。
 * @returns 汇总数据。
 */
export async function fetchUsageSummary(
  workspaceId: string,
  days = 7
): Promise<UsageSummary> {
  const { data } = await api.get<UsageSummary>("/api/usage/summary", {
    params: { workspace_id: workspaceId, days },
  });
  return data;
}
