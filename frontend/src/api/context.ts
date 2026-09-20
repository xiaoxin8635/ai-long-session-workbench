/**
 * 上下文装配预览 API（EchoDesk 前端，M-F4）。
 *
 * 对接 memory-service `POST /api/context/preview`：只跑取数+装配不调 LLM，
 * 返回最终 messages 与各区块 token 用量（排查"记忆为何没注入"类问题）。
 * 注意：workspace_id 在 body（非 query）。
 */
import { api } from "./client";

/** 单区块装配用量。 */
export interface ContextSectionUsage {
  /** 区块标识（system/procedural/semantic/episodic/working/rag/tool_results）。 */
  key: string;
  /** 区块预算（system 为 -1 表示不裁剪）。 */
  budget_tokens: number;
  /** 实际纳入条数。 */
  included: number;
  /** 裁剪丢弃条数。 */
  dropped: number;
  /** 实际 token 占用。 */
  tokens: number;
}

/** 装配预览结果。 */
export interface ContextPreview {
  /** 实际使用的预算 profile 名。 */
  profile: string;
  /** 总窗口。 */
  window_tokens: number;
  /** 区块可用总量（窗口减输出预留）。 */
  available_tokens: number;
  /** 本次装配 token 总和。 */
  total_tokens: number;
  /** 各区块用量（装配顺序）。 */
  sections: ContextSectionUsage[];
  /** 装配产物（OpenAI messages）。 */
  messages: Array<{ role: string; content: string }>;
}

/**
 * 装配上下文预览。
 *
 * @param params - workspaceId/sessionId 必填；query 模拟当前问题；profile 预算档（缺省 default）。
 * @returns 预览结果。
 */
export async function previewContext(params: {
  workspaceId: string;
  sessionId: string;
  query: string;
  profile?: string;
}): Promise<ContextPreview> {
  const { data } = await api.post<ContextPreview>("/api/context/preview", {
    workspace_id: params.workspaceId,
    session_id: params.sessionId,
    query: params.query,
    ...(params.profile ? { profile: params.profile } : {}),
  });
  return data;
}
