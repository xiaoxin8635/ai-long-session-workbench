/**
 * 用量统计页（EchoDesk 前端 · 「宣纸书卷」古风，M-F5）。
 *
 * 读取 `GET /api/usage/summary`：窗口天数切换、总量/会话/轮次概览卡、
 * 记忆/检索/工具三区块 token 拆分占比条、按日趋势柱状图（纯 CSS 实现，
 * 不引入图表库）。
 */
import { useEffect, useState, type JSX } from "react";
import { X } from "lucide-react";
import { fetchUsageSummary } from "../api/usage";
import type { UsageSummary } from "../api/usage";
import { listWorkspaces } from "../api/workspaces";
import { errorMessage } from "../stores/auth";

/** 可选统计窗口（天）。 */
const DAY_OPTIONS = [7, 14, 30, 90];

/**
 * 用量统计页组件（数据自包含，无独立 store）。
 *
 * @returns 页面 JSX。
 */
export default function UsagePage(): JSX.Element {
  /** 汇总数据。 */
  const [summary, setSummary] = useState<UsageSummary | null>(null);
  /** 窗口天数。 */
  const [days, setDays] = useState(7);
  /** 加载中标记。 */
  const [loading, setLoading] = useState(true);
  /** 错误文案。 */
  const [error, setError] = useState<string | null>(null);

  // days 变化或首挂载时拉取汇总
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    listWorkspaces()
      .then(async (workspaces) => {
        const wsId = workspaces[0]?.id;
        if (wsId === undefined) {
          return;
        }
        const data = await fetchUsageSummary(wsId, days);
        if (!cancelled) {
          setSummary(data);
        }
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setError(errorMessage(err));
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [days]);

  const totals = summary?.totals;
  // 区块拆分占比：相对 prompt tokens（区块是装配注入的 prompt 子集）
  const promptTokens = totals?.prompt_tokens ?? 0;
  const sections =
    totals === undefined
      ? []
      : [
          { key: "memory_tokens", label: "记忆", tokens: totals.memory_tokens, color: "bg-accent" },
          { key: "rag_tokens", label: "检索", tokens: totals.rag_tokens, color: "bg-teal" },
          { key: "tool_tokens", label: "工具", tokens: totals.tool_tokens, color: "bg-warning" },
        ];
  // 按日趋势柱高基准
  const maxDayTokens = Math.max(
    1,
    ...(summary?.by_day ?? []).map((d) => d.prompt_tokens + d.completion_tokens)
  );

  return (
    <div className="anim-rise-in mx-auto max-w-4xl space-y-5 p-6">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 font-display text-lg font-semibold text-primary">
          <span className="bookmark-bar h-4" aria-hidden />
          用量
        </h1>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="input rounded-lg px-2.5 py-1.5 text-xs"
          aria-label="统计窗口"
        >
          {DAY_OPTIONS.map((d) => (
            <option key={d} value={d}>
              近 {d} 天
            </option>
          ))}
        </select>
      </div>

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

      {loading && <p className="py-8 text-center font-display text-xs text-muted">加载中…</p>}

      {!loading && summary !== null && (
        <>
          {/* 概览卡 */}
          <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
            <StatCard label="总 token" value={fmt(totals!.prompt_tokens + totals!.completion_tokens)} />
            <StatCard label="Prompt" value={fmt(totals!.prompt_tokens)} />
            <StatCard label="Completion" value={fmt(totals!.completion_tokens)} />
            <StatCard label="会话数" value={fmt(summary.sessions)} />
            <StatCard label="轮次数" value={fmt(summary.turns)} />
          </div>

          {/* 区块拆分 */}
          <section className="rounded-xl border border-line/12 bg-surface/50 p-4 shadow-panel">
            <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
              <span className="bookmark-bar h-3.5" aria-hidden />
              上下文区块拆分（占 prompt 比例）
            </h2>
            <div className="mt-3 space-y-2.5">
              {sections.map((s) => {
                const pct = promptTokens > 0 ? Math.min(100, (s.tokens / promptTokens) * 100) : 0;
                return (
                  <div key={s.key} className="flex items-center gap-3 text-xs">
                    <span className="w-10 shrink-0 text-secondary">{s.label}</span>
                    <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-line/10">
                      <div
                        className={"h-full rounded-full transition-[width] duration-700 ease-brush " + s.color}
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="w-24 shrink-0 text-right font-mono text-muted">
                      {fmt(s.tokens)} · {pct.toFixed(1)}%
                    </span>
                  </div>
                );
              })}
            </div>
          </section>

          {/* 按日趋势 */}
          <section className="rounded-xl border border-line/12 bg-surface/50 p-4 shadow-panel">
            <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
              <span className="bookmark-bar h-3.5" aria-hidden />
              按日趋势
            </h2>
            {summary.by_day.length === 0 ? (
              <p className="mt-3 py-6 text-center font-display text-xs text-muted">窗口内暂无用量数据</p>
            ) : (
              <div className="mt-4 flex h-32 items-end gap-1.5">
                {summary.by_day.map((d) => {
                  const total = d.prompt_tokens + d.completion_tokens;
                  const height = Math.max(4, (total / maxDayTokens) * 100);
                  return (
                    <div key={d.day} className="flex min-w-0 flex-1 flex-col items-center gap-1">
                      <span className="font-mono text-[9px] text-muted">{fmt(total)}</span>
                      <div
                        className="w-full rounded-t bg-accent/75 transition-colors hover:bg-accent"
                        style={{ height: `${height}%` }}
                        title={`${d.day}：prompt ${fmt(d.prompt_tokens)} / completion ${fmt(d.completion_tokens)}`}
                      />
                      <span className="w-full truncate text-center font-mono text-[9px] text-muted">
                        {d.day.slice(5)}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
          </section>
        </>
      )}
    </div>
  );
}

/**
 * 概览统计卡片。
 *
 * @param props - label 指标名；value 格式化后的值。
 * @returns 卡片 JSX。
 */
function StatCard({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div className="card-paper rounded-xl p-3">
      <p className="font-display text-[11px] tracking-wide text-muted">{label}</p>
      <p className="mt-1 font-mono text-lg font-semibold tabular-nums text-primary">{value}</p>
    </div>
  );
}

/**
 * 数字格式化（千分位）。
 *
 * @param n - 原始数值。
 * @returns 千分位字符串。
 */
function fmt(n: number): string {
  return n.toLocaleString();
}
