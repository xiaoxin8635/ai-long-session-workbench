/**
 * ⌘K 命令面板（EchoDesk 前端 · 「宣纸书卷」古风）。
 *
 * 全局快捷入口：模糊搜索并跳转 7 个页面 / 执行「新建对话」等动作。
 * 受控组件（open/onClose 由 AppLayout 持有，配合全局 ⌘K/Ctrl+K 监听）。
 * 键盘：↑↓ 选择、Enter 执行、Esc 关闭；渲染为 role="dialog" 覆盖层，
 * 背景点击关闭，内容区阻止 overscroll 冒泡。
 */
import { AnimatePresence, motion } from "framer-motion";
import {
  Brain,
  CornerDownLeft,
  Gauge,
  Library,
  ListChecks,
  MessageSquare,
  Plus,
  Search,
  Settings,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type JSX, type KeyboardEvent } from "react";
import { useNavigate } from "react-router-dom";
import { useChatStore } from "../stores/chat";

/** 单条命令。 */
interface Command {
  /** 唯一 ID。 */
  id: string;
  /** 展示名。 */
  label: string;
  /** 分组标题。 */
  group: string;
  /** 图标。 */
  icon: LucideIcon;
  /** 模糊匹配关键词（含拼音首字母/英文别名）。 */
  keywords: string;
  /** 执行动作。 */
  run: () => void;
}

/**
 * 命令面板组件。
 *
 * @param props - open 是否展开；onClose 关闭回调。
 * @returns 面板 JSX（关闭时渲染空占位以保留 AnimatePresence 退场）。
 */
export function CommandPalette({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}): JSX.Element {
  const navigate = useNavigate();
  const startDraft = useChatStore((s) => s.startDraft);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  /** 全量命令（导航 + 动作）。 */
  const commands = useMemo<Command[]>(() => {
    const go = (path: string) => (): void => {
      navigate(path);
      onClose();
    };
    return [
      {
        id: "new-chat",
        label: "新建对话",
        group: "动作",
        icon: Plus,
        keywords: "新建对话 new chat xinjian xj",
        run: () => {
          navigate("/");
          startDraft();
          onClose();
        },
      },
      { id: "nav-chat", label: "对话", group: "跳转", icon: MessageSquare, keywords: "对话 chat duihua dh", run: go("/") },
      { id: "nav-memories", label: "记忆", group: "跳转", icon: Brain, keywords: "记忆 memory ji yi jy", run: go("/memories") },
      { id: "nav-knowledge", label: "知识库", group: "跳转", icon: Library, keywords: "知识库 knowledge zhi zhi shi zsk", run: go("/knowledge") },
      { id: "nav-tasks", label: "任务", group: "跳转", icon: ListChecks, keywords: "任务 task ren wu rw", run: go("/tasks") },
      { id: "nav-tools", label: "工具", group: "跳转", icon: Wrench, keywords: "工具 tool gong ju gj", run: go("/tools") },
      { id: "nav-usage", label: "用量", group: "跳转", icon: Gauge, keywords: "用量 usage yong liang yl", run: go("/usage") },
      { id: "nav-settings", label: "设置", group: "跳转", icon: Settings, keywords: "设置 settings she zhi sz", run: go("/settings") },
    ];
  }, [navigate, onClose, startDraft]);

  /** 依 query 过滤（大小写不敏感子串匹配 label/keywords）。 */
  const filtered = useMemo<Command[]>(() => {
    const q = query.trim().toLowerCase();
    if (q === "") {
      return commands;
    }
    return commands.filter(
      (c) => c.label.toLowerCase().includes(q) || c.keywords.toLowerCase().includes(q)
    );
  }, [commands, query]);

  // 展开时重置查询与光标并聚焦输入框
  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      // 等覆盖层挂载后聚焦
      window.setTimeout(() => inputRef.current?.focus(), 0);
    }
  }, [open]);

  // 过滤结果变化时夹紧光标
  useEffect(() => {
    setCursor((c) => Math.min(c, Math.max(0, filtered.length - 1)));
  }, [filtered.length]);

  // 光标项滚入可视区
  useEffect(() => {
    const el = listRef.current?.querySelector<HTMLElement>(`[data-idx="${cursor}"]`);
    el?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  /**
   * 执行指定命令（越界忽略）。
   *
   * @param idx - 命令下标。
   */
  function runAt(idx: number): void {
    const cmd = filtered[idx];
    if (cmd !== undefined) {
      cmd.run();
    }
  }

  /**
   * 面板按键：↑↓ 移动光标、Enter 执行、Esc 关闭。
   *
   * @param e - 键盘事件。
   */
  function handleKeyDown(e: KeyboardEvent<HTMLDivElement>): void {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => (filtered.length === 0 ? 0 : (c + 1) % filtered.length));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => (filtered.length === 0 ? 0 : (c - 1 + filtered.length) % filtered.length));
    } else if (e.key === "Enter") {
      e.preventDefault();
      runAt(cursor);
    } else if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    }
  }

  // 按分组切分（保持 filtered 顺序，组名去重）
  const groups = useMemo(() => {
    const order: string[] = [];
    const map = new Map<string, { cmd: Command; idx: number }[]>();
    filtered.forEach((cmd, idx) => {
      if (!map.has(cmd.group)) {
        map.set(cmd.group, []);
        order.push(cmd.group);
      }
      map.get(cmd.group)!.push({ cmd, idx });
    });
    return order.map((g) => ({ group: g, items: map.get(g)! }));
  }, [filtered]);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          className="fixed inset-0 z-[90] flex items-start justify-center px-4 pt-[16vh]"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
        >
          {/* 背景遮罩（点击关闭） */}
          <div
            className="absolute inset-0 bg-base/70 backdrop-blur-sm"
            onClick={onClose}
            aria-hidden
          />
          <motion.div
            role="dialog"
            aria-modal="true"
            aria-label="命令面板"
            initial={{ opacity: 0, y: -12, scale: 0.98 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.98 }}
            transition={{ type: "spring", stiffness: 400, damping: 32 }}
            onKeyDown={handleKeyDown}
            className="glass relative w-full max-w-lg overflow-hidden rounded-2xl shadow-panel-lg"
            style={{ overscrollBehavior: "contain" }}
          >
            {/* 搜索输入 */}
            <div className="flex items-center gap-2.5 border-b border-line/8 px-4 py-3">
              <Search className="size-4 shrink-0 text-muted" strokeWidth={2.2} aria-hidden />
              <input
                ref={inputRef}
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索页面或动作…"
                aria-label="搜索命令"
                autoComplete="off"
                spellCheck={false}
                className="w-full bg-transparent text-sm text-primary outline-none placeholder:text-muted"
              />
              <kbd className="shrink-0 rounded-md border border-line/10 bg-line/5 px-1.5 py-0.5 font-mono text-[10px] text-muted">
                Esc
              </kbd>
            </div>

            {/* 结果列表 */}
            <div ref={listRef} className="max-h-80 overflow-y-auto p-2">
              {filtered.length === 0 ? (
                <p className="px-3 py-8 text-center text-xs text-muted">无匹配命令</p>
              ) : (
                groups.map(({ group, items }) => (
                  <div key={group} className="mb-1">
                    <p className="px-2.5 py-1 font-display text-[11px] tracking-[0.2em] text-muted">
                      {group}
                    </p>
                    {items.map(({ cmd, idx }) => (
                      <button
                        key={cmd.id}
                        type="button"
                        data-idx={idx}
                        onMouseEnter={() => setCursor(idx)}
                        onClick={() => runAt(idx)}
                        aria-selected={idx === cursor}
                        className={
                          "relative flex w-full items-center gap-3 rounded-lg px-2.5 py-2 text-left text-sm transition-colors " +
                          (idx === cursor
                            ? "bg-accent/12 font-medium text-primary ring-1 ring-inset ring-accent/25"
                            : "text-secondary hover:bg-line/6")
                        }
                      >
                        {idx === cursor && (
                          <span className="bookmark-bar absolute left-0 top-1/2 h-4 -translate-y-1/2" aria-hidden />
                        )}
                        <cmd.icon
                          className={"size-4 shrink-0 " + (idx === cursor ? "text-accent" : "text-muted")}
                          strokeWidth={2}
                          aria-hidden
                        />
                        <span className="flex-1 truncate">{cmd.label}</span>
                        {idx === cursor && (
                          <CornerDownLeft className="size-3.5 shrink-0 text-muted" strokeWidth={2} aria-hidden />
                        )}
                      </button>
                    ))}
                  </div>
                ))
              )}
            </div>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
