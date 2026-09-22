/**
 * 全局 Toast store（EchoDesk 前端 · 「Echo Console」）。
 *
 * 轻量通知条：支持语义色调、可选动作按钮（如「撤销」）、自动消失时长。
 * 用于替代原生 window.confirm/alert——破坏性操作走「乐观执行 + 撤销窗口」，
 * 比模态确认更快也更安全。挂载点见 components/ui/ToastViewport.tsx。
 */
import { create } from "zustand";

/** Toast 语义色调（复用设计 token 语义色）。 */
export type ToastTone = "neutral" | "success" | "danger" | "info";

/** Toast 可选动作按钮（如撤销）。 */
export interface ToastAction {
  /** 按钮文案。 */
  label: string;
  /** 点击回调（点击后自动关闭该 Toast）。 */
  onClick: () => void;
}

/** 单条 Toast。 */
export interface Toast {
  /** 唯一 ID（自增）。 */
  id: number;
  /** 正文。 */
  message: string;
  /** 语义色调。 */
  tone: ToastTone;
  /** 自动消失时长（ms）；0 = 常驻需手动关闭。 */
  duration: number;
  /** 可选动作按钮。 */
  action?: ToastAction;
}

/** push 的入参（id 由 store 分配）。 */
export type ToastInput = Omit<Toast, "id"> & { duration?: number };

/** Toast store 状态与动作。 */
interface ToastState {
  /** 当前展示的 Toast 列表（追加到尾部）。 */
  toasts: Toast[];
  /** 推入一条 Toast，返回其 ID（供命令式关闭）。 */
  push: (input: ToastInput) => number;
  /** 按 ID 关闭。 */
  dismiss: (id: number) => void;
}

/** Toast 自增 ID。 */
let toastSeq = 0;

/** 默认自动消失时长（ms）。 */
const DEFAULT_DURATION = 4000;

export const useToastStore = create<ToastState>()((set, get) => ({
  toasts: [],
  push: (input) => {
    toastSeq += 1;
    const id = toastSeq;
    const toast: Toast = {
      id,
      message: input.message,
      tone: input.tone,
      duration: input.duration ?? DEFAULT_DURATION,
      ...(input.action !== undefined ? { action: input.action } : {}),
    };
    set({ toasts: [...get().toasts, toast] });
    return id;
  },
  dismiss: (id) => {
    set({ toasts: get().toasts.filter((t) => t.id !== id) });
  },
}));

/**
 * 命令式推入 Toast（组件外也可调用）。
 *
 * @param input - Toast 内容。
 * @returns 新 Toast 的 ID。
 */
export function showToast(input: ToastInput): number {
  return useToastStore.getState().push(input);
}
