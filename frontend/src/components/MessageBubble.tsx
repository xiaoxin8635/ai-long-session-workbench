/**
 * 消息气泡（EchoDesk 前端 · 「宣纸书卷」古风）：入场动画 + Markdown 渲染 +
 * 流式光标 + RAG 引用书签 + hover 工具条（复制 / 重新生成）。
 *
 * 用户消息右对齐（竹青渐变实心）；助手消息左对齐（纸面 + 竹青玉印头像 chip）。
 * 工具条在 hover / 键盘聚焦（focus-within）时显现，触屏设备常显；引用来源
 * 为可点击按钮（书签样式），展开显示文件名 / 切片序号 / 相关度明细。
 */
import { motion } from "framer-motion";
import { Check, Copy, FileText, RotateCcw, Sparkles } from "lucide-react";
import { useState, type JSX } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Citation } from "../api/chat";
import { Badge } from "./ui/Badge";
import type { ChatMessage } from "../stores/chat";

/**
 * 单条消息气泡组件。
 *
 * @param props - message 消息对象；streaming 本条是否仍在流式接收；
 *   onRegenerate 重新生成回调（仅最后一条已完成的助手消息传入）；
 *   canRegenerate 是否允许重新生成（流式/挂起确认时为 false）。
 * @returns 气泡 JSX。
 */
export function MessageBubble({
  message,
  streaming,
  onRegenerate,
  canRegenerate = true,
}: {
  message: ChatMessage;
  streaming: boolean;
  onRegenerate?: () => void;
  canRegenerate?: boolean;
}): JSX.Element {
  const isUser = message.role === "user";
  /** 复制成功瞬时反馈。 */
  const [copied, setCopied] = useState(false);
  /** 当前展开明细的引用 chunk_id（null = 无）。 */
  const [openCitation, setOpenCitation] = useState<string | null>(null);

  /**
   * 复制消息正文到剪贴板（1.5s 内显示「已复制」反馈）。
   */
  async function copyContent(): Promise<void> {
    try {
      await navigator.clipboard.writeText(message.content);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板不可用（非安全上下文/权限拒绝）时静默忽略
    }
  }

  /** hover 工具条：复制（用户/助手通用）+ 重新生成（仅助手且提供回调）。 */
  const toolbar =
    !streaming && message.content !== "" ? (
      <div className="mt-1 flex items-center gap-1 opacity-0 transition-opacity duration-200 focus-within:opacity-100 group-hover:opacity-100 [@media(pointer:coarse)]:opacity-100">
        <button
          type="button"
          onClick={() => void copyContent()}
          aria-label={copied ? "已复制" : "复制消息"}
          className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-muted transition-colors hover:bg-line/8 hover:text-primary"
        >
          {copied ? (
            <Check className="size-3 text-success" strokeWidth={2.4} aria-hidden />
          ) : (
            <Copy className="size-3" strokeWidth={2} aria-hidden />
          )}
          {copied ? "已复制" : "复制"}
        </button>
        {!isUser && onRegenerate !== undefined && (
          <button
            type="button"
            onClick={onRegenerate}
            disabled={!canRegenerate}
            aria-label="重新生成回答"
            className="flex items-center gap-1 rounded-md px-1.5 py-1 text-[11px] text-muted transition-colors hover:bg-line/8 hover:text-primary disabled:cursor-not-allowed disabled:opacity-40"
          >
            <RotateCcw className="size-3" strokeWidth={2} aria-hidden />
            重新生成
          </button>
        )}
      </div>
    ) : null;

  if (isUser) {
    return (
      <motion.div
        initial={{ opacity: 0, y: 10, scale: 0.98 }}
        animate={{ opacity: 1, y: 0, scale: 1 }}
        transition={{ duration: 0.26, ease: [0.22, 1, 0.36, 1] }}
        className="group flex flex-col items-end"
      >
        <div className="max-w-3xl rounded-2xl rounded-br-md bg-gradient-to-br from-accent-bright to-accent px-4 py-2.5 text-sm leading-6 text-elevated shadow-panel">
          <p className="whitespace-pre-wrap break-words font-medium">{message.content}</p>
        </div>
        {toolbar}
      </motion.div>
    );
  }

  return (
    <motion.div
      initial={{ opacity: 0, y: 10 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      className="group flex gap-3"
    >
      {/* 助手头像 chip：竹青玉印 */}
      <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-lg bg-accent/12 ring-1 ring-accent/25">
        <Sparkles className="size-3.5 text-accent" strokeWidth={2.2} aria-hidden />
      </span>

      <div className="min-w-0 max-w-3xl flex-1">
        <div className="rounded-2xl rounded-tl-md border border-line/8 bg-surface/70 px-4 py-2.5 text-sm leading-6 text-primary shadow-panel backdrop-blur-sm">
          {/* 流式且尚无内容：三点跳动；有内容：正文 + 闪烁光标 */}
          {streaming && message.content === "" ? (
            <span className="inline-flex gap-1 py-1.5" role="status" aria-label="正在生成回答">
              <i className="size-1.5 animate-bounce rounded-full bg-accent [animation-delay:0ms]" />
              <i className="size-1.5 animate-bounce rounded-full bg-accent [animation-delay:150ms]" />
              <i className="size-1.5 animate-bounce rounded-full bg-accent [animation-delay:300ms]" />
            </span>
          ) : (
            <div className={streaming ? "stream-caret" : undefined}>
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  // 代码块/行内代码样式（Tailwind 直接覆写，避免额外依赖）
                  code: ({ children, className }) => {
                    const isBlock =
                      typeof className === "string" && className.includes("language-");
                    if (isBlock) {
                      return (
                        <code className="block overflow-x-auto rounded-lg border border-line/8 bg-base p-3 font-mono text-xs leading-5 text-secondary">
                          {children}
                        </code>
                      );
                    }
                    return (
                      <code className="rounded bg-line/8 px-1 py-0.5 font-mono text-xs text-accent">
                        {children}
                      </code>
                    );
                  },
                  pre: ({ children }) => <div className="my-2">{children}</div>,
                  a: ({ children, href }) => (
                    <a
                      className="text-accent underline decoration-accent/40 underline-offset-2 transition-colors hover:decoration-accent"
                      href={href}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {children}
                    </a>
                  ),
                }}
              >
                {message.content}
              </ReactMarkdown>
            </div>
          )}

          {message.failed && (
            <p className="mt-1.5 text-xs text-danger">回答生成失败，请重试</p>
          )}

          {message.citations !== undefined && message.citations.length > 0 && (
            <div className="mt-3 border-t border-line/10 pt-2.5">
              <p className="mb-1.5 flex items-center gap-1.5 font-display text-[11px] tracking-[0.18em] text-muted">
                <span className="bookmark-bar h-3" aria-hidden />
                引用来源
              </p>
              <div className="flex flex-wrap gap-1.5">
                {message.citations.map((c: Citation) => {
                  const expanded = openCitation === c.chunk_id;
                  return (
                    <span key={c.chunk_id} className="relative">
                      <button
                        type="button"
                        aria-expanded={expanded}
                        onClick={() => setOpenCitation(expanded ? null : c.chunk_id)}
                        className="transition-transform duration-200 hover:-translate-y-px focus-visible:translate-y-0"
                      >
                        <Badge tone="info" dot={false}>
                          <FileText className="size-3" strokeWidth={2} aria-hidden />
                          <span className="max-w-[12rem] truncate">{c.filename}</span>
                          <span className="font-mono text-info/60">#{c.chunk_index}</span>
                        </Badge>
                      </button>
                      {expanded && (
                        <motion.span
                          initial={{ opacity: 0, y: -4 }}
                          animate={{ opacity: 1, y: 0 }}
                          className="absolute left-0 top-full z-20 mt-1 block w-56 rounded-lg border border-line/10 bg-elevated p-2.5 font-mono text-[10px] leading-4 text-secondary shadow-panel-lg"
                        >
                          <span className="block truncate text-primary">{c.filename}</span>
                          <span className="mt-1 block">切片 #{c.chunk_index}</span>
                          <span className="block">相关度 {c.score.toFixed(3)}</span>
                        </motion.span>
                      )}
                    </span>
                  );
                })}
              </div>
            </div>
          )}
        </div>
        {toolbar}
      </div>
    </motion.div>
  );
}
