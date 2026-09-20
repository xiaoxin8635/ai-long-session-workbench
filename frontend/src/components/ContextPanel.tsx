/**
 * 上下文面板（EchoDesk 前端，M-F4）。
 *
 * 聊天右栏：按当前会话 + 模拟 query 调用 /api/context/preview，
 * 展示各区块 token 预算/纳入/裁剪明细与装配产物（排查"记忆为何没注入"）。
 */
import { useState, type JSX } from "react";
import { previewContext } from "../api/context";
import type { ContextPreview } from "../api/context";
import { errorMessage } from "../stores/auth";
import { useChatStore } from "../stores/chat";

/** 区块名中文标注（未收录的显示原值）。 */
const SECTION_LABELS: Record<string, string> = {
  system: "系统合成",
  procedural: "偏好/流程",
  semantic: "语义事实",
  episodic: "会话摘要",
  working: "工作记忆",
  rag: "知识检索",
  tool_results: "工具结果",
};

/**
 * 上下文面板组件。
 *
 * @returns 面板 JSX。
 */
export function ContextPanel(): JSX.Element {
  const workspaceId = useChatStore((s) => s.workspaceId);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const messages = useChatStore((s) => s.messages);

  /** 模拟 query 草稿（默认预填最后一条用户消息）。 */
  const [query, setQuery] = useState("");
  /** 预算 profile（空 = 服务端 default）。 */
  const [profile, setProfile] = useState("");
  /** 预览结果。 */
  const [result, setResult] = useState<ContextPreview | null>(null);
  /** 请求进行中。 */
  const [busy, setBusy] = useState(false);
  /** 错误提示。 */
  const [error, setError] = useState<string | null>(null);
  /** 是否展开装配产物 JSON。 */
  const [showMessages, setShowMessages] = useState(false);

  /**
   * 发起装配预览（query 为空时回退到最后一条用户消息）。
   */
  async function run(): Promise<void> {
    if (workspaceId === null || activeSessionId === null) {
      return;
    }
    const effectiveQuery =
      query.trim() !== ""
        ? query.trim()
        : [...messages].reverse().find((m) => m.role === "user")?.content ?? "";
    if (effectiveQuery === "") {
      setError("请输入模拟 query（会话中尚无用户消息）");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const preview = await previewContext({
        workspaceId,
        sessionId: activeSessionId,
        query: effectiveQuery,
        profile: profile.trim() === "" ? undefined : profile.trim(),
      });
      setResult(preview);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full flex-col gap-3 overflow-y-auto">
      <h2 className="text-sm font-semibold">上下文面板</h2>

      {activeSessionId === null ? (
        <p className="text-xs leading-5 text-slate-500 dark:text-slate-400">
          选择一个会话后可预览该会话的上下文装配（记忆命中、区块预算与裁剪明细）。
        </p>
      ) : (
        <>
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={2}
            placeholder="模拟 query（空 = 最后一条用户消息）"
            className="w-full resize-none rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
          />
          <div className="flex gap-2">
            <input
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
              placeholder="profile（默认 default）"
              className="min-w-0 flex-1 rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-800"
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => void run()}
              className="shrink-0 rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-sky-700 disabled:opacity-50"
            >
              {busy ? "装配中…" : "装配预览"}
            </button>
          </div>

          {error !== null && (
            <p role="alert" className="text-xs text-red-500">
              {error}
            </p>
          )}

          {result !== null && (
            <div className="space-y-2 text-xs">
              <div className="rounded-lg bg-slate-100 p-2 leading-5 dark:bg-slate-800">
                <p>
                  profile <b>{result.profile}</b> · 窗口 {result.window_tokens} · 可用{" "}
                  {result.available_tokens}
                </p>
                <p>
                  本装配 <b>{result.total_tokens}</b> tokens
                </p>
              </div>
              {result.sections.map((s) => (
                <div key={s.key} className="rounded-lg border border-slate-200 p-2 dark:border-slate-800">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{SECTION_LABELS[s.key] ?? s.key}</span>
                    <span className="text-slate-400">
                      {s.tokens}/{s.budget_tokens === -1 ? "∞" : s.budget_tokens} tok
                    </span>
                  </div>
                  <p className="mt-0.5 text-slate-500 dark:text-slate-400">
                    纳入 {s.included} 条{s.dropped > 0 ? ` · 裁剪 ${s.dropped} 条` : ""}
                  </p>
                </div>
              ))}
              <button
                type="button"
                onClick={() => setShowMessages((v) => !v)}
                className="text-sky-600 hover:underline dark:text-sky-400"
              >
                {showMessages ? "收起装配产物" : "查看装配产物"}
              </button>
              {showMessages && (
                <pre className="max-h-72 overflow-auto rounded-lg bg-slate-950 p-2 text-[10px] leading-4 text-slate-200">
                  {JSON.stringify(result.messages, null, 2)}
                </pre>
              )}
            </div>
          )}
        </>
      )}
    </div>
  );
}
