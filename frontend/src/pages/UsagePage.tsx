/**
 * 用量统计页（EchoDesk 前端，M-F5）。
 *
 * 读取 `GET /api/usage/summary`：窗口天数切换、总量/会话/轮次概览卡、
 * 记忆/检索/工具三区块 token 拆分占比条、按日趋势柱状图（纯 CSS 实现，
 * 不引入图表库）。
 */
import { useEffect, useState, type JSX } from "react";
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
          { key: "memory_tokens", label: "记忆", tokens: totals.memory_tokens, color: "bg-sky-500" },
          { key: "rag_tokens", label: "检索", tokens: totals.rag_tokens, color: "bg-emerald-500" },
          { key: "tool_tokens", label: "工具", tokens: totals.tool_tokens, color: "bg-violet-500" },
        ];
  // 按日趋势柱高基准
  const maxDayTokens = Math.max(
    1,
    ...(summary?.by_day ?? []).map((d) => d.prompt_tokens + d.completion_tokens)
  );

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">用量</h1>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
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
          className="rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300"
        >
          {error}
        </div>
      )}

      {loading && <p className="py-8 text-center text-xs text-slate-400">加载中…</p>}

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
          <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
            <h2 className="text-sm font-semibold">上下文区块拆分（占 prompt 比例）</h2>
            <div className="mt-3 space-y-2.5">
              {sections.map((s) => {
                const pct = promptTokens > 0 ? Math.min(100, (s.tokens / promptTokens) * 100) : 0;
                return (
                  <div key={s.key} className="flex items-center gap-3 text-xs">
                    <span className="w-10 shrink-0 text-slate-500 dark:text-slate-400">{s.label}</span>
                    <div className="h-2.5 flex-1 overflow-hidden rounded-full bg-slate-100 dark:bg-slate-800">
                      <div className={"h-full rounded-full " + s.color} style={{ width: `${pct}%` }} />
                    </div>
                    <span className="w-24 shrink-0 text-right text-slate-400">
                      {fmt(s.tokens)} · {pct.toFixed(1)}%
                    </span>
                  </div>
                );
              })}
            </div>
          </section>

          {/* 按日趋势 */}
          <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
            <h2 className="text-sm font-semibold">按日趋势</h2>
            {summary.by_day.length === 0 ? (
              <p className="mt-3 py-6 text-center text-xs text-slate-400">窗口内暂无用量数据</p>
            ) : (
              <div className="mt-4 flex h-32 items-end gap-1.5">
                {summary.by_day.map((d) => {
                  const total = d.prompt_tokens + d.completion_tokens;
                  const height = Math.max(4, (total / maxDayTokens) * 100);
                  return (
                    <div key={d.day} className="flex min-w-0 flex-1 flex-col items-center gap-1">
                      <span className="text-[9px] text-slate-400">{fmt(total)}</span>
                      <div
                        className="w-full rounded-t bg-sky-500/80 transition hover:bg-sky-600"
                        style={{ height: `${height}%` }}
                        title={`${d.day}：prompt ${fmt(d.prompt_tokens)} / completion ${fmt(d.completion_tokens)}`}
                      />
                      <span className="w-full truncate text-center text-[9px] text-slate-400">
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
    <div className="rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
      <p className="text-[10px] text-slate-400">{label}</p>
      <p className="mt-1 text-lg font-semibold tabular-nums">{value}</p>
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
