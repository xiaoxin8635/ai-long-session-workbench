/**
 * 工具页（EchoDesk 前端 · 「宣纸书卷」古风，M-F3）。
 *
 * 四块：工具清单（含风险分级与参数 schema）、MCP 服务管理（热插拔：
 * 添加即试连注册、移除即断连注销；支持 http/sse/stdio 三 transport，
 * 远程端点可带鉴权请求头接百炼/高德等托管 MCP）、直调执行调试（external
 * 202 待确认时给确认/拒绝按钮）、调用审计（created_at 倒序，可刷新）。
 */
import { useEffect, useState, type JSX } from "react";
import { Plus, Trash2, X } from "lucide-react";
import { RiskBadge } from "../components/ToolConfirmCard";
import * as mcpApi from "../api/mcp";
import type { McpServerInfo } from "../api/mcp";
import * as toolsApi from "../api/tools";
import type { ToolCall, ToolExecutionResult, ToolInfo } from "../api/tools";
import { errorMessage } from "../stores/auth";
import { useChatStore } from "../stores/chat";
import { showToast } from "../stores/toast";

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

  /** MCP server 清单（配置 + 运行态）。 */
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([]);
  /** MCP 添加表单：名称。 */
  const [mcpName, setMcpName] = useState("");
  /** MCP 添加表单：传输方式。 */
  const [mcpTransport, setMcpTransport] = useState<"http" | "sse" | "stdio">("http");
  /** MCP 添加表单：远程端点（http/sse）。 */
  const [mcpUrl, setMcpUrl] = useState("");
  /** MCP 添加表单：请求头 JSON 文本（http/sse 可选，承载 API key 鉴权）。 */
  const [mcpHeadersText, setMcpHeadersText] = useState("");
  /** MCP 添加表单：stdio 命令。 */
  const [mcpCommand, setMcpCommand] = useState("");
  /** MCP 添加表单：stdio 参数（空白分隔）。 */
  const [mcpArgsText, setMcpArgsText] = useState("");
  /** MCP 添加/移除请求进行中。 */
  const [mcpBusy, setMcpBusy] = useState(false);

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
      setMcpServers(await mcpApi.listMcpServers());
      if (workspaceId !== null) {
        setCalls(await toolsApi.listToolCalls(workspaceId));
      }
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  /**
   * 热添加 MCP server：服务端试连成功后工具即刻进入清单。
   */
  async function handleAddMcp(): Promise<void> {
    let headers: Record<string, string> | undefined;
    if (mcpTransport !== "stdio" && mcpHeadersText.trim() !== "") {
      try {
        const parsed: unknown = JSON.parse(mcpHeadersText);
        if (
          typeof parsed !== "object" ||
          parsed === null ||
          Array.isArray(parsed) ||
          !Object.values(parsed).every((v) => typeof v === "string")
        ) {
          setError("请求头须为 JSON 对象（键值均为字符串）");
          return;
        }
        headers = parsed as Record<string, string>;
      } catch {
        setError("请求头不是合法 JSON");
        return;
      }
    }
    setMcpBusy(true);
    setError(null);
    try {
      const payload: mcpApi.McpServerCreatePayload = {
        name: mcpName.trim(),
        transport: mcpTransport,
        ...(mcpTransport === "stdio"
          ? {
              command: mcpCommand.trim(),
              args: mcpArgsText.trim() === "" ? [] : mcpArgsText.trim().split(/\s+/),
            }
          : { url: mcpUrl.trim(), ...(headers !== undefined ? { headers } : {}) }),
      };
      const created = await mcpApi.addMcpServer(payload);
      showToast({
        message: `MCP 服务 ${created.name} 已接入（${created.tools.length} 个工具）`,
        tone: "success",
        duration: 4000,
      });
      setMcpName("");
      setMcpUrl("");
      setMcpHeadersText("");
      setMcpCommand("");
      setMcpArgsText("");
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setMcpBusy(false);
    }
  }

  /**
   * 热移除 MCP server（断连 + 注销其全部工具）。
   *
   * @param name - server 标识。
   */
  async function handleRemoveMcp(name: string): Promise<void> {
    setMcpBusy(true);
    setError(null);
    try {
      await mcpApi.removeMcpServer(name);
      showToast({ message: `MCP 服务 ${name} 已移除`, tone: "neutral", duration: 4000 });
      await refresh();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setMcpBusy(false);
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

      {/* MCP 服务管理（热插拔） */}
      <section>
        <h2 className="mb-2 flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          MCP 服务管理
        </h2>
        <div className="card-paper rounded-xl p-3">
          <div className="grid gap-2">
            {mcpServers.map((s) => (
              <div
                key={s.id}
                className="flex items-center gap-2 rounded-lg border border-line/10 bg-base/40 px-3 py-2 text-xs"
              >
                <span className="font-mono font-medium text-primary">{s.name}</span>
                <span className="rounded-md bg-accent/10 px-1.5 py-0.5 font-mono text-accent">{s.transport}</span>
                <span
                  className={
                    s.connected
                      ? "rounded-md bg-success/10 px-1.5 py-0.5 text-success"
                      : "rounded-md bg-danger/10 px-1.5 py-0.5 text-danger"
                  }
                >
                  {s.connected ? `已连接 · ${s.tools.length} 工具` : "未连接"}
                </span>
                {s.headers !== null && (
                  <span
                    className="rounded-md bg-accent/10 px-1.5 py-0.5 text-accent"
                    title={`请求头：${Object.keys(s.headers).join(", ")}（值已打码）`}
                  >
                    鉴权头
                  </span>
                )}
                <span className="truncate text-muted" title={s.url ?? s.command ?? ""}>
                  {s.source === "env" ? "env 种子" : s.url ?? s.command}
                </span>
                {s.source === "user" && (
                  <button
                    type="button"
                    disabled={mcpBusy}
                    onClick={() => void handleRemoveMcp(s.name)}
                    className="ml-auto rounded-md p-1 text-muted transition-colors hover:bg-danger/10 hover:text-danger disabled:opacity-50"
                    aria-label={`移除 ${s.name}`}
                  >
                    <Trash2 className="size-3.5" strokeWidth={2} />
                  </button>
                )}
              </div>
            ))}
            {mcpServers.length === 0 && (
              <p className="font-display text-xs text-muted">暂无 MCP 服务——添加后其工具即刻注册（external 级，执行需确认）</p>
            )}
          </div>

          {/* 添加表单 */}
          <div className="mt-3 space-y-2 border-t border-line/10 pt-3">
            <div className="flex gap-2">
              <input
                value={mcpName}
                onChange={(e) => setMcpName(e.target.value)}
                placeholder="名称（小写字母/数字/中划线）"
                className="input w-44 rounded-lg px-2.5 py-1.5 font-mono text-xs"
              />
              <select
                value={mcpTransport}
                onChange={(e) => setMcpTransport(e.target.value as "http" | "sse" | "stdio")}
                className="input rounded-lg px-2.5 py-1.5 text-xs"
              >
                <option value="http">http（streamable）</option>
                <option value="sse">sse（托管端点）</option>
                <option value="stdio">stdio（子进程）</option>
              </select>
              {mcpTransport !== "stdio" ? (
                <input
                  value={mcpUrl}
                  onChange={(e) => setMcpUrl(e.target.value)}
                  placeholder={mcpTransport === "sse" ? "https://host/sse" : "http://host:port/mcp"}
                  className="input min-w-0 flex-1 rounded-lg px-2.5 py-1.5 font-mono text-xs"
                />
              ) : (
                <>
                  <input
                    value={mcpCommand}
                    onChange={(e) => setMcpCommand(e.target.value)}
                    placeholder="命令（如 npx）"
                    className="input w-36 rounded-lg px-2.5 py-1.5 font-mono text-xs"
                  />
                  <input
                    value={mcpArgsText}
                    onChange={(e) => setMcpArgsText(e.target.value)}
                    placeholder="参数（空白分隔，如 -y some-mcp-server）"
                    className="input min-w-0 flex-1 rounded-lg px-2.5 py-1.5 font-mono text-xs"
                  />
                </>
              )}
              <button
                type="button"
                disabled={mcpBusy || mcpName.trim() === ""}
                onClick={() => void handleAddMcp()}
                className="btn-primary flex shrink-0 items-center gap-1 rounded-lg px-3 py-1.5 text-xs"
              >
                <Plus className="size-3.5" strokeWidth={2.2} />
                {mcpBusy ? "接入中…" : "添加"}
              </button>
            </div>
            {mcpTransport !== "stdio" && (
              <input
                value={mcpHeadersText}
                onChange={(e) => setMcpHeadersText(e.target.value)}
                placeholder='请求头 JSON（可选，如 {"Authorization": "Bearer sk-..."}）'
                aria-label="请求头 JSON"
                className="input w-full rounded-lg px-2.5 py-1.5 font-mono text-xs"
              />
            )}
            <p className="text-xs text-muted">添加时服务端立即试连并注册工具；连接失败不会保存配置。</p>
          </div>
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
