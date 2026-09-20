/**
 * 消息气泡（EchoDesk 前端）：Markdown 渲染 + RAG 引用来源 + 流式光标。
 */
import type { JSX } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Citation } from "../api/chat";
import type { ChatMessage } from "../stores/chat";

/**
 * 单条消息气泡组件。
 *
 * @param props - message 消息对象；streaming 本条是否仍在流式接收。
 * @returns 气泡 JSX。
 */
export function MessageBubble({
  message,
  streaming,
}: {
  message: ChatMessage;
  streaming: boolean;
}): JSX.Element {
  const isUser = message.role === "user";

  return (
    <div className={"flex " + (isUser ? "justify-end" : "justify-start")}>
      <div
        className={
          "max-w-3xl rounded-2xl px-4 py-2.5 text-sm leading-6 " +
          (isUser
            ? "bg-sky-600 text-white"
            : "bg-white text-slate-900 shadow-sm dark:bg-slate-900 dark:text-slate-100")
        }
      >
        {isUser ? (
          <p className="whitespace-pre-wrap">{message.content}</p>
        ) : (
          <>
            <ReactMarkdown
              remarkPlugins={[remarkGfm]}
              components={{
                // 代码块/行内代码样式（Tailwind 直接覆写，避免 @tailwindcss/typography 依赖）
                code: ({ children, className }) => {
                  const isBlock = typeof className === "string" && className.includes("language-");
                  if (isBlock) {
                    return (
                      <code className="block overflow-x-auto rounded-lg bg-slate-950 p-3 text-xs leading-5 text-slate-100">
                        {children}
                      </code>
                    );
                  }
                  return (
                    <code className="rounded bg-slate-100 px-1 py-0.5 text-xs dark:bg-slate-800">
                      {children}
                    </code>
                  );
                },
                pre: ({ children }) => <div className="my-2">{children}</div>,
                a: ({ children, href }) => (
                  <a
                    className="text-sky-600 underline dark:text-sky-400"
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
            {streaming && message.content === "" && (
              <span className="inline-flex gap-1 py-1" aria-label="生成中">
                <i className="size-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:0ms]" />
                <i className="size-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:150ms]" />
                <i className="size-1.5 animate-bounce rounded-full bg-slate-400 [animation-delay:300ms]" />
              </span>
            )}
            {message.failed && (
              <p className="mt-1 text-xs text-red-500">回答生成失败，请重试</p>
            )}
            {message.citations !== undefined && message.citations.length > 0 && (
              <div className="mt-2 border-t border-slate-200 pt-2 dark:border-slate-700">
                <p className="mb-1 text-xs font-medium text-slate-500">引用来源</p>
                <div className="flex flex-wrap gap-1.5">
                  {message.citations.map((c: Citation) => (
                    <span
                      key={c.chunk_id}
                      title={`chunk #${c.chunk_index} · 相关度 ${c.score.toFixed(3)}`}
                      className="rounded-full bg-slate-100 px-2 py-0.5 text-xs text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                    >
                      📄 {c.filename}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
