/**
 * 工具页（EchoDesk 前端 · 「宣纸书卷」古风，M-F3）。
 *
 * 三块：工具清单（含风险分级与参数 schema）、直调执行调试（external 202
 * 待确认时给确认/拒绝按钮）、调用审计（created_at 倒序，可刷新）。
 */
import { useEffect, useState, type JSX } from "react";
import { X } from "lucide-react";
import { RiskBadge } from "../components/ToolConfirmCard";
import * as toolsApi from "../api/tools";
import type { ToolCall, ToolExecutionResult, ToolInfo } from "../api/tools";
import { errorMessage } from "../stores/auth";
import { useChatStore } from "../stores/chat";

/**
 * 工具页组件。
 *
 * @returns 工具页 JSX。
 */
export default function ToolsPage(): JSX.Element {
  const workspaceId = useChatStore((s) => s.workspaceId);
  const bootstrap = useChatStore((s) => s.bootstrap);

  /** 工具清单。 */
  const [tools, setTools] = useState<ToolInfo[]>([]);
  /** 调用审计。 */
  const [calls, setCalls] = useState<ToolCall[]>([]);
  /** 页面级错误提示。 */
  const [error, setError] = useState<string | null>(null);

  /** 直调执行表单选中的工具名。 */
  const [execTool, setExecTool] = useState<string>("");
  /** 直调执行参数 JSON 文本。 */
  const [execArgs, setExecArgs] = useState("{}");
  /** 最近一次直调执行结果。 */
  const [execResult, setExecResult] = useState<ToolExecutionResult | null>(null);
  /** 执行/确认请求进行中。 */
  const [busy, setBusy] = useState(false);

  // workspace 未就绪时先引导 bootstrap（从登录直达本页的场景）
  useEffect(() => {
    if (workspaceId === null) {
      void bootstrap();
    }
  }, [workspaceId, bootstrap]);

  /** 拉取工具清单与审计（workspaceId 变化后由 effect 重新触发）。 */
  async function refresh(): Promise<void> {
    try {
      const toolList = await toolsApi.listTools();
      setTools(toolList);
      setExecTool((prev) => (prev === "" && toolList.length > 0 ? toolList[0]!.name : prev));
      if (workspaceId !== null) {
        setCalls(await toolsApi.listToolCalls(workspaceId));
      }
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refresh 闭包已捕获当次渲染的 workspaceId，随 deps 变化重建
  }, [workspaceId]);

  /**
   * 提交直调执行。
   */
  async function handleExecute(): Promise<void> {
    if (workspaceId === null) {
      return;
    }
    let args: Record<string, unknown>;
    try {
      args = JSON.parse(execArgs) as Record<string, unknown>;
    } catch {
      setError("参数不是合法 JSON");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await toolsApi.executeTool(workspaceId, execTool, args);
      setExecResult(result);
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  /**
   * 裁决直调执行产生的 pending 调用（终态由后台落审计，裁决后刷新）。
   *
   * @param approve - true 执行 / false 拒绝。
   */
  async function handleConfirm(approve: boolean): Promise<void> {
    if (workspaceId === null || execResult === null) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await toolsApi.confirmToolCall(workspaceId, execResult.call_id, approve);
      setExecResult(null);
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="anim-rise-in mx-auto max-w-4xl space-y-6 p-6">
      <h1 className="flex items-center gap-2 font-display text-lg font-semibold text-primary">
        <span className="bookmark-bar h-4" aria-hidden />
        工具
      </h1>

      {error !== null && (
        <div
          role="alert"
          className="flex items-center justify-between rounded-lg border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger"
        >
          <span className="truncate">{error}</span>
          <button type="button" onClick={() => setError(null)} className="ml-3 text-danger" aria-label="关闭错误提示">
            <X className="size-3.5" strokeWidth={2.2} />
          </button>
        </div>
      )}

      {/* 工具清单 */}
      <section>
        <h2 className="mb-2 flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          已注册工具
        </h2>
        <div className="grid gap-2">
          {tools.map((t, i) => (
            <div
              key={t.name}
              className="card-paper anim-rise-in rounded-xl p-3 text-sm"
              style={{ animationDelay: `${Math.min(i, 12) * 30}ms` }}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono font-medium text-primary">{t.name}</span>
                <RiskBadge risk={t.risk} />
              </div>
              <p className="mt-1 text-xs leading-5 text-secondary">{t.description}</p>
            </div>
          ))}
          {tools.length === 0 && <p className="font-display text-xs text-muted">暂无工具</p>}
        </div>
      </section>

      {/* 直调执行调试 */}
      <section>
        <h2 className="mb-2 flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          直调执行（调试）
        </h2>
        <div className="flex gap-2">
          <select
            value={execTool}
            onChange={(e) => setExecTool(e.target.value)}
            className="input rounded-lg px-2.5 py-1.5 text-sm"
          >
            {tools.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
          <input
            value={execArgs}
            onChange={(e) => setExecArgs(e.target.value)}
            placeholder='{"url": "https://example.com"}'
            className="input min-w-0 flex-1 rounded-lg px-2.5 py-1.5 font-mono text-sm"
          />
          <button
            type="button"
            disabled={busy || workspaceId === null || execTool === ""}
            onClick={() => void handleExecute()}
            className="btn-primary shrink-0 rounded-lg px-3 py-1.5 text-sm"
          >
            执行
          </button>
        </div>
        {execResult !== null && (
          <div className="card-paper mt-2 rounded-xl p-3 text-xs">
            <p className="text-secondary">
              call_id：<code className="font-mono text-primary">{execResult.call_id}</code> · status：{execResult.status}
            </p>
            {execResult.requires_confirmation ? (
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleConfirm(true)}
                  className="btn-primary rounded-lg px-3 py-1.5 text-xs"
                >
                  确认执行
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleConfirm(false)}
                  className="btn-ghost rounded-lg px-3 py-1.5 text-xs"
                >
                  拒绝
                </button>
              </div>
            ) : (
              <pre className="mt-2 max-h-40 overflow-auto rounded-lg border border-line/10 bg-base/60 p-2.5 font-mono text-xs leading-5 text-secondary">
                {execResult.error ?? JSON.stringify(execResult.result, null, 2)}
              </pre>
            )}
          </div>
        )}
      </section>

      {/* 调用审计 */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
            <span className="bookmark-bar h-3.5" aria-hidden />
            调用审计
          </h2>
          <button
            type="button"
            onClick={() => void refresh()}
            className="rounded-md px-2 py-0.5 text-xs text-accent transition-colors hover:bg-accent/10"
          >
            刷新
          </button>
        </div>
        <div className="overflow-x-auto rounded-xl border border-line/12 shadow-panel">
          <table className="w-full text-left text-xs">
            <thead className="bg-accent/8 text-secondary">
              <tr>
                <th className="px-3 py-2 font-medium">时间</th>
                <th className="px-3 py-2 font-medium">工具</th>
                <th className="px-3 py-2 font-medium">风险</th>
                <th className="px-3 py-2 font-medium">状态</th>
                <th className="px-3 py-2 font-medium">结果摘要</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line/10 bg-surface/60 text-secondary">
              {calls.map((c) => (
                <tr key={c.id} className="transition-colors hover:bg-accent/6">
                  <td className="whitespace-nowrap px-3 py-2 font-mono">{new Date(c.created_at).toLocaleString()}</td>
                  <td className="px-3 py-2 font-mono font-medium text-primary">{c.tool_name}</td>
                  <td className="px-3 py-2">
                    <RiskBadge risk={c.risk_level} />
                  </td>
                  <td className="px-3 py-2">{c.status}</td>
                  <td className="max-w-xs truncate px-3 py-2 text-muted" title={c.result_digest ?? ""}>
                    {c.result_digest ?? "-"}
                  </td>
                </tr>
              ))}
              {calls.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-4 text-center font-display text-muted">
                    暂无调用记录
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
