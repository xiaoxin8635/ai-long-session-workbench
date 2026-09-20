/**
 * 工具确认卡片 + 风险徽标（EchoDesk 前端，M-F3）。
 *
 * Agent 图流式回答中收到 external 工具确认请求时，在消息流内渲染原生
 * 同意/拒绝按钮（替代 Open WebUI Pipe 的确认词文本协议）。
 */
import type { JSX } from "react";
import type { ToolCallRequest } from "../api/chat";
import type { ToolRisk } from "../api/tools";

/** 风险分级 → 徽标样式映射。 */
const RISK_STYLES: Record<ToolRisk, string> = {
  read_only: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400",
  write: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-400",
  external: "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-400",
};

/** 风险分级 → 中文名称。 */
const RISK_LABELS: Record<ToolRisk, string> = {
  read_only: "只读",
  write: "写入",
  external: "需确认",
};

/**
 * 风险分级徽标。
 *
 * @param props - risk 风险分级。
 * @returns 徽标 JSX。
 */
export function RiskBadge({ risk }: { risk: ToolRisk }): JSX.Element {
  return (
    <span
      className={
        "rounded px-1.5 py-0.5 text-[10px] font-medium " + (RISK_STYLES[risk] ?? RISK_STYLES.write)
      }
    >
      {RISK_LABELS[risk] ?? risk}
    </span>
  );
}

/**
 * 工具确认卡片组件。
 *
 * @param props - toolCall 确认请求；busy 裁决请求进行中；onApprove/onDeny 裁决回调。
 * @returns 卡片 JSX。
 */
export function ToolConfirmCard({
  toolCall,
  busy,
  onApprove,
  onDeny,
}: {
  toolCall: ToolCallRequest;
  busy: boolean;
  onApprove: () => void;
  onDeny: () => void;
}): JSX.Element {
  /** 参数 JSON 预览（截断防刷屏）。 */
  const argsPreview = JSON.stringify(toolCall.args, null, 2).slice(0, 400);

  return (
    <div className="mx-auto w-full max-w-md rounded-xl border border-amber-300 bg-amber-50 p-3 text-sm dark:border-amber-700 dark:bg-amber-950">
      <div className="flex items-center gap-2">
        <span aria-hidden>🔧</span>
        <span className="font-medium">{toolCall.tool}</span>
        <RiskBadge risk={toolCall.risk as ToolRisk} />
      </div>
      <pre className="mt-2 max-h-40 overflow-auto rounded-lg bg-white/70 p-2 text-xs leading-5 text-slate-700 dark:bg-slate-900 dark:text-slate-300">
        {argsPreview}
      </pre>
      <p className="mt-2 text-xs text-slate-500 dark:text-slate-400">
        该工具为外部风险动作，需要你的确认后才会执行。
      </p>
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={onApprove}
          className="rounded-lg bg-emerald-600 px-3 py-1.5 text-xs font-medium text-white
                     transition hover:bg-emerald-700 disabled:opacity-50"
        >
          同意执行
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onDeny}
          className="rounded-lg bg-slate-200 px-3 py-1.5 text-xs font-medium text-slate-700
                     transition hover:bg-slate-300 disabled:opacity-50
                     dark:bg-slate-800 dark:text-slate-300 dark:hover:bg-slate-700"
        >
          拒绝
        </button>
      </div>
    </div>
  );
}
