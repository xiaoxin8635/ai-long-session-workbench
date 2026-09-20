/**
 * api/types 纯函数单测（EchoDesk 前端）。
 */
import { describe, expect, it } from "vitest";
import { isProblem } from "../api/types";

describe("isProblem", () => {
  it("完整 problem+json 结构返回 true", () => {
    const body = {
      type: "https://echodesk.errors/permission_denied",
      title: "permission_denied",
      status: 403,
      detail: "workspace 不存在或无访问权限",
      trace_id: "abc",
    };
    expect(isProblem(body)).toBe(true);
  });

  it("非对象与缺字段返回 false", () => {
    expect(isProblem(null)).toBe(false);
    expect(isProblem("boom")).toBe(false);
    expect(isProblem({ detail: "只有 detail" })).toBe(false);
  });
});
