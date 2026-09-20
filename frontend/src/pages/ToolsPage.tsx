/**
 * 工具页（EchoDesk 前端，M-F3）。
 *
 * 三块：工具清单（含风险分级与参数 schema）、直调执行调试（external 202
 * 待确认时给确认/拒绝按钮）、调用审计（created_at 倒序，可刷新）。
 */
import { useEffect, useState, type JSX } from "react";
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
    <div className="mx-auto max-w-4xl space-y-6 p-6">
      <h1 className="text-lg font-semibold">工具</h1>

      {error !== null && (
        <div role="alert" className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300">
          {error}
        </div>
      )}

      {/* 工具清单 */}
      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-700 dark:text-slate-300">已注册工具</h2>
        <div className="grid gap-2">
          {tools.map((t) => (
            <div
              key={t.name}
              className="rounded-xl border border-slate-200 bg-white p-3 text-sm dark:border-slate-800 dark:bg-slate-900"
            >
              <div className="flex items-center gap-2">
                <span className="font-medium">{t.name}</span>
                <RiskBadge risk={t.risk} />
              </div>
              <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">{t.description}</p>
            </div>
          ))}
          {tools.length === 0 && <p className="text-xs text-slate-400">暂无工具</p>}
        </div>
      </section>

      {/* 直调执行调试 */}
      <section>
        <h2 className="mb-2 text-sm font-semibold text-slate-700 dark:text-slate-300">直调执行（调试）</h2>
        <div className="flex gap-2">
          <select
            value={execTool}
            onChange={(e) => setExecTool(e.target.value)}
            className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
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
            className="flex-1 rounded-lg border border-slate-300 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900"
          />
          <button
            type="button"
            disabled={busy || workspaceId === null || execTool === ""}
            onClick={() => void handleExecute()}
            className="rounded-lg bg-sky-600 px-3 py-1.5 text-sm font-medium text-white transition hover:bg-sky-700 disabled:opacity-50"
          >
            执行
          </button>
        </div>
        {execResult !== null && (
          <div className="mt-2 rounded-xl border border-slate-200 bg-white p-3 text-xs dark:border-slate-800 dark:bg-slate-900">
            <p>
              call_id：<code>{execResult.call_id}</code> · status：{execResult.status}
            </p>
            {execResult.requires_confirmation ? (
              <div className="mt-2 flex gap-2">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleConfirm(true)}
                  className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-emerald-700 disabled:opacity-50"
                >
                  确认执行
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => void handleConfirm(false)}
                  className="rounded-lg bg-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700 transition hover:bg-slate-300 disabled:opacity-50 dark:bg-slate-800 dark:text-slate-300"
                >
                  拒绝
                </button>
              </div>
            ) : (
              <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-slate-50 p-2 leading-5 text-slate-700 dark:bg-slate-800 dark:text-slate-300">
                {execResult.error ?? JSON.stringify(execResult.result, null, 2)}
              </pre>
            )}
          </div>
        )}
      </section>

      {/* 调用审计 */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-700 dark:text-slate-300">调用审计</h2>
          <button
            type="button"
            onClick={() => void refresh()}
            className="text-xs text-sky-600 hover:underline dark:text-sky-400"
          >
            刷新
          </button>
        </div>
        <div className="overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800">
          <table className="w-full text-left text-xs">
            <thead className="bg-slate-50 text-slate-500 dark:bg-slate-900 dark:text-slate-400">
              <tr>
                <th className="px-3 py-2 font-medium">时间</th>
                <th className="px-3 py-2 font-medium">工具</th>
                <th className="px-3 py-2 font-medium">风险</th>
                <th className="px-3 py-2 font-medium">状态</th>
                <th className="px-3 py-2 font-medium">结果摘要</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100 bg-white dark:divide-slate-800 dark:bg-slate-900 dark:text-slate-300">
              {calls.map((c) => (
                <tr key={c.id}>
                  <td className="whitespace-nowrap px-3 py-2">{new Date(c.created_at).toLocaleString()}</td>
                  <td className="px-3 py-2 font-medium">{c.tool_name}</td>
                  <td className="px-3 py-2">
                    <RiskBadge risk={c.risk_level} />
                  </td>
                  <td className="px-3 py-2">{c.status}</td>
                  <td className="max-w-xs truncate px-3 py-2 text-slate-500 dark:text-slate-400" title={c.result_digest ?? ""}>
                    {c.result_digest ?? "-"}
                  </td>
                </tr>
              ))}
              {calls.length === 0 && (
                <tr>
                  <td colSpan={5} className="px-3 py-3 text-center text-slate-400">
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
