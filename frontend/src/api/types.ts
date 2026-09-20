/**
 * 后端契约公共类型（EchoDesk 前端）。
 *
 * 与 memory-service 的 RFC 7807 problem+json 错误结构对齐
 * （见 services/memory-service/app/core/errors.py）。
 */

/** RFC 7807 问题详情（后端统一错误响应体）。 */
export interface Problem {
  /** 错误类型 URI（https://echodesk.errors/<code>）。 */
  type: string;
  /** 机器可读错误码（如 permission_denied）。 */
  title: string;
  /** HTTP 状态码。 */
  status: number;
  /** 面向用户的中文错误描述。 */
  detail: string;
  /** 服务端 trace_id（Langfuse 可审计）。 */
  trace_id?: string;
}

/** 判断未知错误对象是否携带 Problem 结构。 */
export function isProblem(value: unknown): value is Problem {
  return (
    typeof value === "object" &&
    value !== null &&
    "title" in value &&
    "status" in value &&
    "detail" in value
  );
}
