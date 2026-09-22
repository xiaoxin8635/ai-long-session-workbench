/**
 * 工具确认卡片 + 风险徽标（EchoDesk 前端 · 「宣纸书卷」古风，M-F3）。
 *
 * Agent 图流式回答中收到 external 工具确认请求时，在消息流内渲染弹簧入场的
 * 同意/拒绝卡片；RiskBadge 复用统一 Badge 系统（供本页与 ToolsPage 共用）。
 */
import { motion } from "framer-motion";
import { ShieldAlert, Wrench } from "lucide-react";
import type { JSX } from "react";
import type { ToolCallRequest } from "../api/chat";
import type { ToolRisk } from "../api/tools";
import { Badge, type BadgeTone } from "./ui/Badge";

/** 风险分级 → 徽章色调映射。 */
const RISK_TONES: Record<ToolRisk, BadgeTone> = {
  read_only: "success",
  write: "warning",
  external: "danger",
};

/** 风险分级 → 中文名称。 */
const RISK_LABELS: Record<ToolRisk, string> = {
  read_only: "只读",
  write: "写入",
  external: "需确认",
};

/**
 * 风险分级徽标（统一 Badge 系统）。
 *
 * @param props - risk 风险分级。
 * @returns 徽标 JSX。
 */
export function RiskBadge({ risk }: { risk: ToolRisk }): JSX.Element {
  const tone = RISK_TONES[risk] ?? "warning";
  return (
    <Badge tone={tone}>
      {RISK_LABELS[risk] ?? risk}
    </Badge>
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
    <motion.div
      initial={{ opacity: 0, y: 12, scale: 0.97 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      transition={{ type: "spring", stiffness: 320, damping: 26 }}
      className="ml-10 w-full max-w-md overflow-hidden rounded-2xl border border-warning/30 bg-warning/[0.06] shadow-panel backdrop-blur-sm"
    >
      {/* 顶部标题条 */}
      <div className="flex items-center gap-2.5 border-b border-warning/20 px-4 py-3">
        <span className="flex size-8 items-center justify-center rounded-lg bg-warning/15 ring-1 ring-warning/25">
          <ShieldAlert className="size-4 text-warning" strokeWidth={2.2} />
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <Wrench className="size-3.5 shrink-0 text-secondary" strokeWidth={2} />
            <span className="truncate font-mono text-sm font-medium text-primary">
              {toolCall.tool}
            </span>
          </div>
        </div>
        <RiskBadge risk={toolCall.risk as ToolRisk} />
      </div>

      <div className="px-4 py-3">
        <pre className="max-h-40 overflow-auto rounded-lg border border-line/8 bg-base/60 p-2.5 font-mono text-xs leading-5 text-secondary">
          {argsPreview}
        </pre>
        <p className="mt-2.5 text-xs leading-5 text-secondary">
          该工具为外部风险动作，需要你的确认后才会执行。
        </p>
        <div className="mt-3 flex gap-2">
          <motion.button
            type="button"
            whileTap={{ scale: 0.96 }}
            disabled={busy}
            onClick={onApprove}
            className="btn-primary flex-1 rounded-xl px-3 py-2 text-xs"
          >
            同意执行
          </motion.button>
          <motion.button
            type="button"
            whileTap={{ scale: 0.96 }}
            disabled={busy}
            onClick={onDeny}
            className="btn-ghost flex-1 rounded-xl px-3 py-2 text-xs font-medium"
          >
            拒绝
          </motion.button>
        </div>
      </div>
    </motion.div>
  );
}
