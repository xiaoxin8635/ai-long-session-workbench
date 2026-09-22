/**
 * 统一徽章/标签系统（EchoDesk 前端 · 「宣纸书卷」古风）。
 *
 * 全站唯一的标签元件，承载记忆类型 / 记忆状态 / 工具风险级 / 任意分类标记，
 * 以 tone（语义色）驱动配色：微着色底 + 墨发丝环 + 前导发光色点 + 等宽可选。
 * 配色基于古风 token（竹青/石绿/赭黄/朱砂/黛蓝）。
 */
import type { JSX, ReactNode } from "react";

/** 徽章语义色调（映射 tailwind.config 的 token 色）。 */
export type BadgeTone =
  | "accent"
  | "info"
  | "violet"
  | "teal"
  | "success"
  | "warning"
  | "danger"
  | "neutral";

/** tone → 配色类（底/环/文字同色系，透明度分层保证宣纸底上清晰）。 */
const TONE_CLASSES: Record<BadgeTone, string> = {
  accent: "bg-accent/12 text-accent ring-accent/30",
  info: "bg-info/12 text-info ring-info/30",
  violet: "bg-violet/12 text-violet ring-violet/30",
  teal: "bg-teal/12 text-teal ring-teal/30",
  success: "bg-success/14 text-success ring-success/30",
  warning: "bg-warning/14 text-warning ring-warning/30",
  danger: "bg-danger/12 text-danger ring-danger/30",
  neutral: "bg-line/6 text-secondary ring-line/12",
};

/**
 * 徽章组件。
 *
 * @param props - tone 语义色（默认 neutral）；dot 是否显示前导发光色点（默认 true）；
 *   mono 是否用等宽字体（key/数值类标记，默认 false）；children 文案；className 追加样式。
 * @returns 徽章 JSX。
 */
export function Badge({
  tone = "neutral",
  dot = true,
  mono = false,
  children,
  className = "",
}: {
  tone?: BadgeTone;
  dot?: boolean;
  mono?: boolean;
  children: ReactNode;
  className?: string;
}): JSX.Element {
  return (
    <span
      className={
        "badge ring-1 ring-inset " +
        TONE_CLASSES[tone] +
        (mono ? " font-mono tracking-tight" : "") +
        (className !== "" ? " " + className : "")
      }
    >
      {dot && <i className="badge-dot" aria-hidden />}
      {children}
    </span>
  );
}
