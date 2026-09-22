/**
 * 全局 Toast 视口（EchoDesk 前端 · 「宣纸书卷」古风）。
 *
 * 固定于右下角，渲染 useToastStore 中的通知条：弹簧入场 + 淡出退场，
 * 每条按 duration 独立计时自动消失；含 aria-live="polite" 供读屏播报，
 * 动作按钮（如「撤销」）点击后触发回调并关闭。挂载在应用根部（App.tsx）。
 */
import { AnimatePresence, motion } from "framer-motion";
import { AlertTriangle, CheckCircle2, Info, X } from "lucide-react";
import { useEffect, type JSX } from "react";
import { useToastStore, type Toast, type ToastTone } from "../../stores/toast";

/** tone → 图标组件。 */
const TONE_ICONS: Record<ToastTone, typeof Info> = {
  neutral: Info,
  success: CheckCircle2,
  danger: AlertTriangle,
  info: Info,
};

/** tone → 配色类（图标/边 ring）。 */
const TONE_CLASSES: Record<ToastTone, string> = {
  neutral: "text-secondary ring-line/12",
  success: "text-success ring-success/25",
  danger: "text-danger ring-danger/25",
  info: "text-info ring-info/25",
};

/**
 * 单条 Toast：负责自身的自动消失计时。
 *
 * @param props - toast 数据。
 * @returns Toast 条目 JSX。
 */
function ToastItem({ toast }: { toast: Toast }): JSX.Element {
  const dismiss = useToastStore((s) => s.dismiss);
  const Icon = TONE_ICONS[toast.tone];

  // duration>0 时自动消失（卸载/时长变化时清理计时器）
  useEffect(() => {
    if (toast.duration <= 0) {
      return;
    }
    const timer = window.setTimeout(() => dismiss(toast.id), toast.duration);
    return () => window.clearTimeout(timer);
  }, [toast.id, toast.duration, dismiss]);

  return (
    <motion.div
      layout
      initial={{ opacity: 0, y: 16, scale: 0.96 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.15 } }}
      transition={{ type: "spring", stiffness: 380, damping: 30 }}
      className={
        "glass pointer-events-auto flex items-center gap-2.5 rounded-xl px-3.5 py-2.5 shadow-panel-lg ring-1 " +
        TONE_CLASSES[toast.tone]
      }
      style={{ overscrollBehavior: "contain" }}
    >
      <Icon className="size-4 shrink-0" strokeWidth={2.2} aria-hidden />
      <span className="max-w-xs text-xs leading-5 text-primary">{toast.message}</span>
      {toast.action !== undefined && (
        <button
          type="button"
          onClick={() => {
            toast.action?.onClick();
            dismiss(toast.id);
          }}
          className="shrink-0 rounded-md px-2 py-1 text-xs font-semibold text-accent transition-colors hover:bg-accent/12"
        >
          {toast.action.label}
        </button>
      )}
      <button
        type="button"
        onClick={() => dismiss(toast.id)}
        aria-label="关闭提示"
        className="shrink-0 rounded-md p-1 text-muted transition-colors hover:bg-line/8 hover:text-primary"
      >
        <X className="size-3.5" strokeWidth={2.2} />
      </button>
    </motion.div>
  );
}

/**
 * Toast 视口容器（aria-live 区域）。
 *
 * @returns 视口 JSX。
 */
export function ToastViewport(): JSX.Element {
  const toasts = useToastStore((s) => s.toasts);
  return (
    <div
      aria-live="polite"
      aria-relevant="additions text"
      className="pointer-events-none fixed bottom-4 right-4 z-[100] flex flex-col items-end gap-2"
    >
      <AnimatePresence initial={false}>
        {toasts.map((t) => (
          <ToastItem key={t.id} toast={t} />
        ))}
      </AnimatePresence>
    </div>
  );
}
