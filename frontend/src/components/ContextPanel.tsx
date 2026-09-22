/**
 * 上下文面板（EchoDesk 前端 · 「宣纸书卷」古风，M-F4）。
 *
 * 聊天右栏：按当前会话 + 模拟 query 调用 /api/context/preview，
 * 以动画预算条展示各区块 token 预算/纳入/裁剪明细与装配产物
 * （排查"记忆为何没注入"）。
 */
import { motion } from "framer-motion";
import { Layers, Play } from "lucide-react";
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
      <h2 className="flex shrink-0 items-center gap-2 font-display text-sm font-semibold text-primary">
        <Layers className="size-4 text-accent" strokeWidth={2.2} />
        上下文面板
      </h2>

      {activeSessionId === null ? (
        <p className="rounded-xl border border-line/8 bg-surface/40 p-3 text-xs leading-5 text-secondary">
          选择一个会话后可预览该会话的上下文装配（记忆命中、区块预算与裁剪明细）。
        </p>
      ) : (
        <>
          <textarea
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            rows={2}
            aria-label="模拟 query"
            placeholder="模拟 query（空 = 最后一条用户消息）"
            className="input w-full shrink-0 resize-none rounded-lg px-2.5 py-2 text-xs leading-5"
          />
          <div className="flex shrink-0 gap-2">
            <input
              value={profile}
              onChange={(e) => setProfile(e.target.value)}
              aria-label="预算 profile"
              placeholder="profile（默认 default）"
              className="input min-w-0 flex-1 rounded-lg px-2.5 py-2 font-mono text-xs"
            />
            <motion.button
              type="button"
              whileTap={{ scale: 0.95 }}
              disabled={busy}
              onClick={() => void run()}
              className="btn-primary flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-2 text-xs"
            >
              <Play className="size-3" strokeWidth={2.4} />
              {busy ? "装配中…" : "装配预览"}
            </motion.button>
          </div>

          {error !== null && (
            <p role="alert" className="text-xs text-danger">
              {error}
            </p>
          )}

          {result !== null && (
            <motion.div
              initial={{ opacity: 0, y: 8 }}
              animate={{ opacity: 1, y: 0 }}
              className="space-y-2 text-xs"
            >
              {/* 总览 */}
              <div className="rounded-xl border border-line/8 bg-surface/50 p-3">
                <div className="flex items-center justify-between font-mono text-[11px] text-secondary">
                  <span>
                    profile <b className="text-accent">{result.profile}</b>
                  </span>
                  <span className="text-muted">
                    win {result.window_tokens} · avail {result.available_tokens}
                  </span>
                </div>
                <div className="mt-2 flex items-end justify-between">
                  <span className="text-muted">本装配</span>
                  <span className="font-mono text-lg font-semibold text-primary">
                    {result.total_tokens}
                    <span className="ml-1 text-xs text-muted">tok</span>
                  </span>
                </div>
              </div>

              {/* 各区块预算条 */}
              {result.sections.map((s, i) => {
                const unlimited = s.budget_tokens === -1;
                const pct = unlimited
                  ? s.tokens > 0
                    ? 100
                    : 0
                  : Math.min(100, Math.round((s.tokens / Math.max(1, s.budget_tokens)) * 100));
                return (
                  <motion.div
                    key={s.key}
                    initial={{ opacity: 0, x: -6 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ delay: i * 0.04 }}
                    className="rounded-xl border border-line/8 bg-surface/40 p-2.5"
                  >
                    <div className="flex items-center justify-between">
                      <span className="font-medium text-primary">
                        {SECTION_LABELS[s.key] ?? s.key}
                      </span>
                      <span className="font-mono text-[11px] text-muted">
                        <b className="text-secondary">{s.tokens}</b>
                        {unlimited ? " / ∞" : ` / ${s.budget_tokens}`}
                      </span>
                    </div>
                    {/* 预算占用条（宽度动画；足额/超额转赭黄→朱砂） */}
                    <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-line/10">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${pct}%` }}
                        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1], delay: i * 0.04 }}
                        className={
                          "h-full rounded-full " +
                          (pct >= 100
                            ? "bg-gradient-to-r from-warning to-danger"
                            : "bg-gradient-to-r from-accent to-accent-bright")
                        }
                      />
                    </div>
                    <p className="mt-1 font-mono text-[10px] text-muted">
                      纳入 {s.included} 条{s.dropped > 0 ? ` · 裁剪 ${s.dropped} 条` : ""}
                    </p>
                  </motion.div>
                );
              })}

              <button
                type="button"
                onClick={() => setShowMessages((v) => !v)}
                className="text-accent transition-colors hover:text-accent-bright"
              >
                {showMessages ? "收起装配产物" : "查看装配产物"}
              </button>
              {showMessages && (
                <pre className="max-h-72 overflow-auto rounded-lg border border-line/8 bg-base p-2.5 font-mono text-[10px] leading-4 text-secondary">
                  {JSON.stringify(result.messages, null, 2)}
                </pre>
              )}
            </motion.div>
          )}
        </>
      )}
    </div>
  );
}
