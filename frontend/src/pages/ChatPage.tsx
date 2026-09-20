/**
 * 对话页（EchoDesk 前端，M-F2）。
 *
 * 三栏布局：会话列表（240px，新建/选择/重命名/删除）+ 消息区（历史回放 +
 * SSE 流式追加 + 输入框）+ 右侧上下文面板（320px，M-F4 充实）。
 */
import { useEffect, useRef, useState, type JSX, type KeyboardEvent } from "react";
import { ContextPanel } from "../components/ContextPanel";
import { MessageBubble } from "../components/MessageBubble";
import { ToolConfirmCard } from "../components/ToolConfirmCard";
import type { SessionRead } from "../api/sessions";
import { useChatStore } from "../stores/chat";

/** 空态引导的快捷提问样例。 */
const SUGGESTIONS = [
  "总结一下我们最近聊过的项目进展",
  "帮我记住：我习惯用 Python 3.11 写后端",
  "上周讨论的部署方案有哪些要点？",
];

/**
 * 对话页组件。
 *
 * @returns 对话页 JSX。
 */
export default function ChatPage(): JSX.Element {
  const workspaceId = useChatStore((s) => s.workspaceId);
  const sessions = useChatStore((s) => s.sessions);
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const messages = useChatStore((s) => s.messages);
  const streaming = useChatStore((s) => s.streaming);
  const error = useChatStore((s) => s.error);
  const bootstrap = useChatStore((s) => s.bootstrap);
  const selectSession = useChatStore((s) => s.selectSession);
  const startDraft = useChatStore((s) => s.startDraft);
  const sendMessage = useChatStore((s) => s.sendMessage);
  const resumeToolCall = useChatStore((s) => s.resumeToolCall);
  const renameSession = useChatStore((s) => s.renameSession);
  const removeSession = useChatStore((s) => s.removeSession);
  const clearError = useChatStore((s) => s.clearError);

  /** 输入框草稿。 */
  const [draft, setDraft] = useState("");
  /** 正在重命名的会话 ID（null = 无）。 */
  const [editingId, setEditingId] = useState<string | null>(null);
  /** 重命名输入框的草稿值。 */
  const [editValue, setEditValue] = useState("");
  /** 工具裁决请求进行中（确认卡片按钮禁用）。 */
  const [resuming, setResuming] = useState(false);

  /** 消息区滚动容器（新消息/流式增量时滚到底部）。 */
  const scrollRef = useRef<HTMLDivElement>(null);
  /** 输入框（自动增高）。 */
  const inputRef = useRef<HTMLTextAreaElement>(null);

  // 首次挂载拉取 workspace 与会话列表（zustand 动作引用稳定，仅执行一次）
  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  // 消息变化（含流式增量）时滚动到底部
  useEffect(() => {
    const el = scrollRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  // 草稿变化时输入框自动增高（上限 160px 后内部滚动）
  useEffect(() => {
    const el = inputRef.current;
    if (el) {
      el.style.height = "0px";
      el.style.height = Math.min(el.scrollHeight, 160) + "px";
    }
  }, [draft]);

  /** 当前选中会话的元信息（标题/token 总量）。 */
  const activeSession: SessionRead | undefined = sessions.find(
    (s) => s.id === activeSessionId
  );

  /** 当前挂起待裁决的 external 工具调用（有挂起时禁输入，直至裁决完成）。 */
  const pendingCall =
    messages.find((m) => m.toolCall !== undefined && m.toolCall.resolved === undefined)
      ?.toolCall ?? null;

  /**
   * 提交当前草稿（流式中、有挂起确认或草稿为空时忽略）。
   */
  function submit(): void {
    const content = draft.trim();
    if (content === "" || streaming || workspaceId === null || pendingCall !== null) {
      return;
    }
    setDraft("");
    void sendMessage(content);
  }

  /**
   * 裁决挂起的工具调用（同意/拒绝后经 resume 续传回答）。
   *
   * @param approve - true 执行 / false 拒绝。
   */
  async function decide(approve: boolean): Promise<void> {
    setResuming(true);
    try {
      await resumeToolCall(approve);
    } finally {
      setResuming(false);
    }
  }

  /**
   * 输入框按键：Enter 发送、Shift+Enter 换行（中文输入法组合中不触发）。
   *
   * @param e - 键盘事件。
   */
  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>): void {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  /**
   * 提交重命名（空值或未变化时取消）。
   */
  function commitRename(): void {
    if (editingId === null) {
      return;
    }
    const title = editValue.trim();
    const original = sessions.find((s) => s.id === editingId)?.title;
    setEditingId(null);
    if (title !== "" && title !== original) {
      void renameSession(editingId, title);
    }
  }

  /**
   * 删除会话（经确认后调用）。
   *
   * @param sessionId - 会话 ID。
   * @param title - 会话标题（用于确认文案）。
   */
  function handleRemove(sessionId: string, title: string): void {
    if (window.confirm(`删除会话「${title}」？历史消息将不可恢复。`)) {
      void removeSession(sessionId);
    }
  }

  return (
    <div className="flex h-full">
      {/* 会话列表栏 */}
      <aside
        className="flex w-60 shrink-0 flex-col border-r border-slate-200 bg-slate-50
                   dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="p-3">
          <button
            type="button"
            onClick={startDraft}
            className="w-full rounded-lg bg-sky-600 px-3 py-2 text-sm font-medium text-white
                       transition hover:bg-sky-700 disabled:opacity-50"
          >
            ＋ 新建对话
          </button>
        </div>
        <ul className="flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
          {sessions.length === 0 && (
            <li className="px-3 py-2 text-xs text-slate-400">暂无会话</li>
          )}
          {sessions.map((s) =>
            editingId === s.id ? (
              <li key={s.id}>
                <input
                  autoFocus
                  value={editValue}
                  onChange={(e) => setEditValue(e.target.value)}
                  onBlur={commitRename}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      commitRename();
                    } else if (e.key === "Escape") {
                      setEditingId(null);
                    }
                  }}
                  className="w-full rounded-md border border-sky-400 px-2 py-1.5 text-sm
                             outline-none dark:bg-slate-800"
                />
              </li>
            ) : (
              <li key={s.id} className="group relative">
                <button
                  type="button"
                  disabled={streaming}
                  onClick={() => void selectSession(s.id)}
                  className={
                    "w-full truncate rounded-md px-3 py-2 pr-12 text-left text-sm transition " +
                    (s.id === activeSessionId
                      ? "bg-white font-medium text-slate-900 shadow-sm dark:bg-slate-800 dark:text-slate-100"
                      : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800")
                  }
                  title={s.title}
                >
                  {s.title}
                </button>
                {/* 悬停操作：重命名 / 删除 */}
                <span className="absolute right-2 top-1/2 hidden -translate-y-1/2 gap-1 group-hover:flex">
                  <button
                    type="button"
                    aria-label={`重命名 ${s.title}`}
                    className="rounded p-1 text-xs text-slate-400 hover:text-sky-600"
                    onClick={() => {
                      setEditingId(s.id);
                      setEditValue(s.title);
                    }}
                  >
                    ✏️
                  </button>
                  <button
                    type="button"
                    aria-label={`删除 ${s.title}`}
                    className="rounded p-1 text-xs text-slate-400 hover:text-red-600"
                    onClick={() => handleRemove(s.id, s.title)}
                  >
                    🗑️
                  </button>
                </span>
              </li>
            )
          )}
        </ul>
      </aside>

      {/* 消息区 */}
      <section className="flex min-w-0 flex-1 flex-col">
        {/* 会话头 */}
        <header
          className="flex items-center justify-between border-b border-slate-200 px-5 py-3
                     dark:border-slate-800"
        >
          <h1 className="truncate text-sm font-semibold">
            {activeSession?.title ?? "新对话"}
          </h1>
          {activeSession && (
            <span className="shrink-0 text-xs text-slate-400">
              {activeSession.token_total} tokens
            </span>
          )}
        </header>

        {/* 消息流 */}
        <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto px-5 py-4">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <p className="text-4xl" aria-hidden>
                💬
              </p>
              <h2 className="mt-3 text-lg font-semibold">开始新的对话</h2>
              <p className="mt-1 text-sm text-slate-500 dark:text-slate-400">
                EchoDesk 会自动检索你的长期记忆与知识库作为上下文
              </p>
              <div className="mt-5 flex flex-col gap-2">
                {SUGGESTIONS.map((sug) => (
                  <button
                    key={sug}
                    type="button"
                    onClick={() => setDraft(sug)}
                    className="rounded-full border border-slate-200 px-4 py-1.5 text-xs
                               text-slate-600 transition hover:border-sky-400 hover:text-sky-700
                               dark:border-slate-700 dark:text-slate-300 dark:hover:border-sky-500"
                  >
                    {sug}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => (
              <div key={m.key} className="space-y-3">
                <MessageBubble
                  message={m}
                  streaming={streaming && i === messages.length - 1 && m.role === "assistant"}
                />
                {m.toolCall !== undefined && m.toolCall.resolved === undefined && (
                  <ToolConfirmCard
                    toolCall={m.toolCall}
                    busy={resuming}
                    onApprove={() => void decide(true)}
                    onDeny={() => void decide(false)}
                  />
                )}
              </div>
            ))
          )}
        </div>

        {/* 错误条 */}
        {error !== null && (
          <div
            role="alert"
            className="mx-5 mb-2 flex items-center justify-between rounded-lg bg-red-50
                       px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300"
          >
            <span className="truncate">{error}</span>
            <button
              type="button"
              onClick={clearError}
              className="ml-3 shrink-0 text-red-500 hover:text-red-700"
              aria-label="关闭错误提示"
            >
              ✕
            </button>
          </div>
        )}

        {/* 输入区 */}
        <footer className="border-t border-slate-200 p-3 dark:border-slate-800">
          <div className="flex items-end gap-2">
            <textarea
              ref={inputRef}
              rows={1}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              disabled={streaming || pendingCall !== null || workspaceId === null}
              placeholder={
                pendingCall !== null
                  ? "等待工具确认…"
                  : streaming
                    ? "回答生成中…"
                    : "输入消息，Enter 发送，Shift+Enter 换行"
              }
              className="max-h-40 flex-1 resize-none rounded-xl border border-slate-300 px-3 py-2
                         text-sm outline-none transition focus:border-sky-500 disabled:bg-slate-100
                         dark:border-slate-700 dark:bg-slate-900 dark:disabled:bg-slate-800"
            />
            <button
              type="button"
              onClick={submit}
              disabled={
                streaming || pendingCall !== null || draft.trim() === "" || workspaceId === null
              }
              className="shrink-0 rounded-xl bg-sky-600 px-4 py-2 text-sm font-medium text-white
                         transition hover:bg-sky-700 disabled:opacity-50"
            >
              {streaming ? "发送中…" : "发送"}
            </button>
          </div>
        </footer>
      </section>

      {/* 右侧上下文面板（记忆命中/区块预算/装配产物明细） */}
      <aside
        className="hidden w-80 shrink-0 border-l border-slate-200 bg-slate-50 p-4
                   dark:border-slate-800 dark:bg-slate-900 xl:block"
      >
        <ContextPanel />
      </aside>
    </div>
  );
}
