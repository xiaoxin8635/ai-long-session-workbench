/**
 * 对话页（EchoDesk 前端 · 「宣纸书卷」古风，M-F2）。
 *
 * 三栏布局：会话列表（玻璃侧栏，新建/选择/重命名/删除）+ 消息区（历史回放 +
 * SSE 流式追加 + 悬浮输入区）+ 右侧上下文面板（token 预算装配明细）。
 */
import { AnimatePresence, motion } from "framer-motion";
import {
  AlertTriangle,
  ArrowDown,
  FileText,
  Loader2,
  MessageSquare,
  PanelRight,
  Paperclip,
  Pencil,
  Plus,
  Send,
  Sparkles,
  Square,
  Trash2,
  X,
} from "lucide-react";
import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type JSX,
  type KeyboardEvent,
} from "react";
import { ContextPanel } from "../components/ContextPanel";
import { MessageBubble } from "../components/MessageBubble";
import { ToolConfirmCard } from "../components/ToolConfirmCard";
import { Badge } from "../components/ui/Badge";
import {
  ACCEPT_EXTENSIONS,
  ALLOWED_EXTENSIONS,
  MAX_UPLOAD_BYTES,
  uploadKnowledgeFile,
} from "../api/knowledge";
import type { SessionRead } from "../api/sessions";
import { errorMessage } from "../stores/auth";
import { useChatStore, type ChatAttachment } from "../stores/chat";
import { showToast } from "../stores/toast";

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
  const stopStreaming = useChatStore((s) => s.stopStreaming);
  const regenerate = useChatStore((s) => s.regenerate);
  const clearError = useChatStore((s) => s.clearError);

  /** 输入框草稿。 */
  const [draft, setDraft] = useState("");
  /** 正在重命名的会话 ID（null = 无）。 */
  const [editingId, setEditingId] = useState<string | null>(null);
  /** 重命名输入框的草稿值。 */
  const [editValue, setEditValue] = useState("");
  /** 工具裁决请求进行中（确认卡片按钮禁用）。 */
  const [resuming, setResuming] = useState(false);
  /** 上下文面板抽屉开关（窄屏 <xl 用）。 */
  const [contextOpen, setContextOpen] = useState(false);
  /** 是否显示「滚到底部」悬浮按钮（用户上滚阅读时）。 */
  const [showScrollBtn, setShowScrollBtn] = useState(false);
  /** 延迟删除中的会话 ID（乐观隐藏 + 撤销窗口；null = 无）。 */
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  /** 本轮已上传、待随消息发送的附件（知识文件）。 */
  const [attachments, setAttachments] = useState<ChatAttachment[]>([]);
  /** 附件上传进行中（禁用发送与再次选择）。 */
  const [uploading, setUploading] = useState(false);

  /** 消息区滚动容器（新消息/流式增量时滚到底部）。 */
  const scrollRef = useRef<HTMLDivElement>(null);
  /** 输入框（自动增高）。 */
  const inputRef = useRef<HTMLTextAreaElement>(null);
  /** 隐藏的附件文件选择器。 */
  const fileInputRef = useRef<HTMLInputElement>(null);
  /** 是否贴底（决定新内容是否自动滚动，避免打断上滚阅读）。 */
  const stickRef = useRef(true);
  /** 延迟删除计时器（撤销时清除）。 */
  const deleteTimerRef = useRef<number | null>(null);

  // 首次挂载拉取 workspace 与会话列表（zustand 动作引用稳定，仅执行一次）
  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  // 消息变化（含流式增量）时——仅当用户贴底才自动滚动，避免打断上滚阅读
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [messages]);

  // 切换会话：重置贴底态并收起悬浮按钮
  useEffect(() => {
    stickRef.current = true;
    setShowScrollBtn(false);
  }, [activeSessionId]);

  // 卸载时清理延迟删除计时器
  useEffect(() => {
    return () => {
      if (deleteTimerRef.current !== null) {
        window.clearTimeout(deleteTimerRef.current);
      }
    };
  }, []);

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
   * 提交当前草稿（流式中、上传中、有挂起确认或内容为空时忽略）。
   *
   * 仅有附件而无文字时用一句中性提示占位（服务端消息正文不可为空）。
   */
  function submit(): void {
    const text = draft.trim();
    const content = text !== "" ? text : attachments.length > 0 ? "请看我上传的附件。" : "";
    if (content === "" || streaming || uploading || workspaceId === null || pendingCall !== null) {
      return;
    }
    const outgoing = attachments;
    setDraft("");
    setAttachments([]);
    stickRef.current = true; // 发送新消息强制贴底
    void sendMessage(content, outgoing.length > 0 ? outgoing : undefined);
  }

  /**
   * 选择文件后立即上传到知识库（复用 ingest 管线），成功后加入待发附件列表。
   *
   * @param e - 文件选择器 change 事件。
   */
  async function handleFiles(e: ChangeEvent<HTMLInputElement>): Promise<void> {
    const files = Array.from(e.target.files ?? []);
    e.target.value = ""; // 清空以便再次选择同名文件仍能触发 change
    if (files.length === 0 || workspaceId === null) {
      return;
    }
    setUploading(true);
    try {
      for (const file of files) {
        const ext = `.${file.name.split(".").pop()?.toLowerCase() ?? ""}`;
        if (!ALLOWED_EXTENSIONS.includes(ext)) {
          showToast({ message: `不支持的文件类型 ${ext}`, tone: "danger", duration: 4000 });
          continue;
        }
        if (file.size > MAX_UPLOAD_BYTES) {
          showToast({ message: `${file.name} 超过 20MB 上限`, tone: "danger", duration: 4000 });
          continue;
        }
        try {
          const uploaded = await uploadKnowledgeFile(workspaceId, file);
          setAttachments((prev) =>
            prev.some((a) => a.id === uploaded.id)
              ? prev
              : [...prev, { id: uploaded.id, filename: uploaded.filename }]
          );
        } catch (err) {
          showToast({
            message: `${file.name} 上传失败：${errorMessage(err)}`,
            tone: "danger",
            duration: 4000,
          });
        }
      }
    } finally {
      setUploading(false);
    }
  }

  /**
   * 移除一个待发附件（仅从本轮列表剔除，不删知识库文件）。
   *
   * @param id - 知识文件 ID。
   */
  function removeAttachment(id: string): void {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
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
   * 删除会话（乐观隐藏 + 5s 撤销窗口；窗口过后才真正调用删除 API）。
   *
   * @param sessionId - 会话 ID。
   * @param title - 会话标题（用于撤销提示文案）。
   */
  function handleRemove(sessionId: string, title: string): void {
    const wasActive = activeSessionId === sessionId;
    setPendingDeleteId(sessionId);
    if (wasActive) {
      startDraft();
    }
    deleteTimerRef.current = window.setTimeout(() => {
      deleteTimerRef.current = null;
      setPendingDeleteId(null);
      void removeSession(sessionId);
    }, 5000);
    showToast({
      message: `已删除会话「${title}」`,
      tone: "danger",
      duration: 5000,
      action: {
        label: "撤销",
        onClick: () => {
          if (deleteTimerRef.current !== null) {
            window.clearTimeout(deleteTimerRef.current);
            deleteTimerRef.current = null;
          }
          setPendingDeleteId(null);
          if (wasActive) {
            void selectSession(sessionId);
          }
        },
      },
    });
  }

  /**
   * 消息区滚动：更新贴底态与「滚到底部」按钮可见性。
   */
  function handleScroll(): void {
    const el = scrollRef.current;
    if (!el) {
      return;
    }
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const near = distance < 80;
    stickRef.current = near;
    setShowScrollBtn(!near);
  }

  /**
   * 平滑滚到底部（悬浮按钮点击）。
   */
  function scrollToBottom(): void {
    const el = scrollRef.current;
    if (!el) {
      return;
    }
    stickRef.current = true;
    el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    setShowScrollBtn(false);
  }

  return (
    <div className="flex h-full">
      {/* ---- 会话列表栏 ---- */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-line/8 bg-elevated/40">
        <div className="p-3">
          <motion.button
            type="button"
            whileTap={{ scale: 0.97 }}
            onClick={startDraft}
            className="btn-primary flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-sm"
          >
            <Plus className="size-4" strokeWidth={2.4} />
            新建对话
          </motion.button>
        </div>
        <ul className="flex-1 space-y-1 overflow-y-auto px-2 pb-3">
          {sessions.length === 0 && (
            <li className="px-3 py-6 text-center text-xs text-muted">暂无会话</li>
          )}
          {sessions.filter((s) => s.id !== pendingDeleteId).map((s) =>
            editingId === s.id ? (
              <li key={s.id} className="px-1">
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
                  className="input w-full rounded-lg px-2.5 py-2 text-sm"
                />
              </li>
            ) : (
              <li key={s.id} className="group relative">
                <button
                  type="button"
                  disabled={streaming}
                  onClick={() => void selectSession(s.id)}
                  className={
                    "relative flex w-full items-center gap-2.5 truncate rounded-lg px-3 py-2.5 pr-11 text-left text-sm transition-colors duration-200 " +
                    (s.id === activeSessionId
                      ? "bg-surface text-primary ring-1 ring-inset ring-accent/20"
                      : "text-secondary hover:bg-surface/60 hover:text-primary")
                  }
                  title={s.title}
                >
                  {s.id === activeSessionId && (
                    <motion.span
                      layoutId="session-active-bar"
                      transition={{ type: "spring", stiffness: 400, damping: 34 }}
                      className="absolute left-0 top-1/2 h-4 w-[3px] -translate-y-1/2 rounded-r-full bg-accent"
                    />
                  )}
                  <MessageSquare
                    className={
                      "size-3.5 shrink-0 " +
                      (s.id === activeSessionId ? "text-accent" : "text-muted")
                    }
                    strokeWidth={2}
                  />
                  <span className="truncate">{s.title}</span>
                </button>
                {/* 悬停操作：重命名 / 删除 */}
                <span className="absolute right-2 top-1/2 flex -translate-y-1/2 gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 group-focus-within:opacity-100 [@media(pointer:coarse)]:opacity-100">
                  <button
                    type="button"
                    aria-label={`重命名 ${s.title}`}
                    className="rounded-md p-1.5 text-muted transition-colors hover:bg-info/15 hover:text-info"
                    onClick={() => {
                      setEditingId(s.id);
                      setEditValue(s.title);
                    }}
                  >
                    <Pencil className="size-3.5" strokeWidth={2} />
                  </button>
                  <button
                    type="button"
                    aria-label={`删除 ${s.title}`}
                    className="rounded-md p-1.5 text-muted transition-colors hover:bg-danger/15 hover:text-danger"
                    onClick={() => handleRemove(s.id, s.title)}
                  >
                    <Trash2 className="size-3.5" strokeWidth={2} />
                  </button>
                </span>
              </li>
            )
          )}
        </ul>
      </aside>

      {/* ---- 消息区 ---- */}
      <section className="relative flex min-w-0 flex-1 flex-col">
        {/* 会话头 */}
        <header className="flex items-center justify-between border-b border-line/8 bg-elevated/30 px-6 py-3.5 backdrop-blur-sm">
          <h1 className="truncate font-display text-[1rem] font-semibold text-primary">
            {activeSession?.title ?? "新对话"}
          </h1>
          <div className="flex shrink-0 items-center gap-2">
            {activeSession && (
              <Badge tone="neutral" mono dot={false}>
                {activeSession.token_total.toLocaleString()} tok
              </Badge>
            )}
            <button
              type="button"
              onClick={() => setContextOpen(true)}
              aria-label="打开上下文面板"
              title="上下文面板"
              className="rounded-lg p-1.5 text-muted transition-colors hover:bg-line/8 hover:text-primary xl:hidden"
            >
              <PanelRight className="size-4" strokeWidth={2} />
            </button>
          </div>
        </header>

        {/* 消息流 */}
        <div ref={scrollRef} onScroll={handleScroll} className="flex-1 space-y-4 overflow-y-auto px-6 py-5">
          {messages.length === 0 ? (
            <div className="flex h-full flex-col items-center justify-center text-center">
              <motion.div
                initial={{ opacity: 0, scale: 0.9 }}
                animate={{ opacity: 1, scale: 1 }}
                transition={{ duration: 0.4, ease: [0.22, 1, 0.36, 1] }}
                className="relative mb-5 flex size-16 items-center justify-center"
              >
                <span className="absolute inset-0 animate-brand-breathe rounded-2xl bg-accent/20 blur-lg" />
                <span className="seal-mark relative flex size-16 items-center justify-center rounded-2xl text-4xl shadow-panel-lg">
                  <span className="relative z-10">憶</span>
                </span>
              </motion.div>
              <motion.h2
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.05 }}
                className="brush-underline pb-1.5 font-display text-xl font-semibold text-primary"
              >
                开始新的对话
              </motion.h2>
              <motion.p
                initial={{ opacity: 0, y: 8 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ delay: 0.1 }}
                className="mt-2 max-w-sm text-sm leading-6 text-secondary"
              >
                EchoDesk 会自动检索你的长期记忆与知识库作为上下文
              </motion.p>
              <div className="mt-6 flex flex-col gap-2.5">
                {SUGGESTIONS.map((sug, i) => (
                  <motion.button
                    key={sug}
                    type="button"
                    initial={{ opacity: 0, y: 10 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.15 + i * 0.07 }}
                    whileHover={{ x: 4 }}
                    whileTap={{ scale: 0.98 }}
                    onClick={() => setDraft(sug)}
                    className="group flex items-center gap-2.5 rounded-full border border-line/10 bg-surface/50 px-4 py-2 text-left text-xs text-secondary transition-colors hover:border-accent/30 hover:bg-accent/5 hover:text-primary"
                  >
                    <Sparkles className="size-3.5 shrink-0 text-muted transition-colors group-hover:text-accent" />
                    {sug}
                  </motion.button>
                ))}
              </div>
            </div>
          ) : (
            messages.map((m, i) => {
              const isLast = i === messages.length - 1;
              const isLastAssistant = isLast && m.role === "assistant";
              return (
                <div key={m.key} className="space-y-4">
                  <MessageBubble
                    message={m}
                    streaming={streaming && isLastAssistant}
                    onRegenerate={isLastAssistant ? () => void regenerate() : undefined}
                    canRegenerate={!streaming && pendingCall === null && workspaceId !== null}
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
              );
            })
          )}
        </div>

        {/* 滚到底部悬浮按钮（用户上滚阅读时出现） */}
        <AnimatePresence>
          {showScrollBtn && (
            <motion.button
              type="button"
              onClick={scrollToBottom}
              aria-label="滚动到最新消息"
              initial={{ opacity: 0, scale: 0.8, y: 8 }}
              animate={{ opacity: 1, scale: 1, y: 0 }}
              exit={{ opacity: 0, scale: 0.8, y: 8 }}
              className="absolute bottom-32 left-1/2 z-10 flex size-9 -translate-x-1/2 items-center justify-center rounded-full border border-line/10 bg-surface/90 text-secondary shadow-panel backdrop-blur-md transition-colors hover:text-accent"
            >
              <ArrowDown className="size-4" strokeWidth={2.2} />
            </motion.button>
          )}
        </AnimatePresence>

        {/* 错误条 */}
        <AnimatePresence>
          {error !== null && (
            <motion.div
              role="alert"
              initial={{ opacity: 0, y: 8, height: 0 }}
              animate={{ opacity: 1, y: 0, height: "auto" }}
              exit={{ opacity: 0, y: 8, height: 0 }}
              className="mx-6 mb-2 flex items-center gap-2 overflow-hidden rounded-xl border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger"
            >
              <AlertTriangle className="size-4 shrink-0" strokeWidth={2} />
              <span className="flex-1 truncate">{error}</span>
              <button
                type="button"
                onClick={clearError}
                className="shrink-0 rounded p-0.5 transition-colors hover:bg-danger/20"
                aria-label="关闭错误提示"
              >
                <X className="size-3.5" />
              </button>
            </motion.div>
          )}
        </AnimatePresence>

        {/* ---- 悬浮输入区 ---- */}
        <footer className="p-4">
          {/* 待发附件 chips（选择文件后立即上传，随下一条消息发送） */}
          <AnimatePresence initial={false}>
            {(attachments.length > 0 || uploading) && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                className="mb-2 flex flex-wrap gap-2 overflow-hidden px-1"
              >
                {attachments.map((a) => (
                  <span
                    key={a.id}
                    className="flex max-w-[220px] items-center gap-1.5 rounded-lg border border-line/10 bg-elevated/60 py-1 pl-2 pr-1 text-xs text-secondary"
                  >
                    <FileText className="size-3.5 shrink-0 text-accent" strokeWidth={2} />
                    <span className="truncate">{a.filename}</span>
                    <button
                      type="button"
                      onClick={() => removeAttachment(a.id)}
                      aria-label={`移除附件 ${a.filename}`}
                      className="shrink-0 rounded p-0.5 transition-colors hover:bg-danger/20 hover:text-danger"
                    >
                      <X className="size-3" />
                    </button>
                  </span>
                ))}
                {uploading && (
                  <span className="flex items-center gap-1.5 rounded-lg border border-line/10 bg-elevated/60 px-2 py-1 text-xs text-muted">
                    <Loader2 className="size-3.5 animate-spin" />
                    上传中…
                  </span>
                )}
              </motion.div>
            )}
          </AnimatePresence>

          <div
            className={
              "group flex items-end gap-2 rounded-2xl border border-line/10 bg-surface/70 p-2 backdrop-blur-md transition-[border-color,box-shadow] duration-300 " +
              "focus-within:border-accent/40 focus-within:shadow-glow"
            }
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept={ACCEPT_EXTENSIONS}
              className="hidden"
              onChange={(e) => void handleFiles(e)}
            />
            <motion.button
              type="button"
              whileTap={{ scale: 0.9 }}
              onClick={() => fileInputRef.current?.click()}
              disabled={uploading || streaming || pendingCall !== null || workspaceId === null}
              aria-label="添加附件"
              title="添加附件（pdf / docx / md / txt / csv / xlsx，≤20MB）"
              className="flex size-9 shrink-0 items-center justify-center rounded-xl border border-line/10 text-secondary transition-colors duration-200 hover:border-accent/40 hover:text-accent disabled:cursor-not-allowed disabled:opacity-40"
            >
              <Paperclip className="size-4" strokeWidth={2} />
            </motion.button>
            <textarea
              ref={inputRef}
              rows={1}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={handleKeyDown}
              aria-label="消息输入框"
              disabled={streaming || pendingCall !== null || workspaceId === null}
              placeholder={
                pendingCall !== null
                  ? "等待工具确认…"
                  : streaming
                    ? "回答生成中…"
                    : "输入消息，Enter 发送，Shift+Enter 换行…"
              }
              className="max-h-40 flex-1 resize-none bg-transparent px-2.5 py-2 text-sm leading-6 text-primary outline-none placeholder:text-muted disabled:cursor-not-allowed"
            />
            {streaming ? (
              <motion.button
                type="button"
                whileTap={{ scale: 0.9 }}
                onClick={stopStreaming}
                aria-label="停止生成"
                title="停止生成"
                className="flex size-9 shrink-0 items-center justify-center rounded-xl border border-danger/30 bg-danger/15 text-danger transition-colors duration-200 hover:bg-danger/25"
              >
                <Square className="size-3.5" strokeWidth={0} fill="currentColor" />
              </motion.button>
            ) : (
              <motion.button
                type="button"
                whileTap={{ scale: 0.9 }}
                onClick={submit}
                disabled={
                  pendingCall !== null ||
                  uploading ||
                  (draft.trim() === "" && attachments.length === 0) ||
                  workspaceId === null
                }
                aria-label="发送"
                className="btn-primary flex size-9 shrink-0 items-center justify-center rounded-xl"
              >
                <Send className="size-4" strokeWidth={2.2} />
              </motion.button>
            )}
          </div>
          <p className="mt-2 px-1 text-center font-mono text-[10px] text-muted">
            Enter 发送 · Shift+Enter 换行 · 可附文件/表格 · 记忆与知识自动注入上下文
          </p>
        </footer>
      </section>

      {/* ---- 右侧上下文面板（xl+ 静态栏） ---- */}
      <aside className="hidden w-80 shrink-0 border-l border-line/8 bg-elevated/40 p-4 xl:block">
        <ContextPanel />
      </aside>

      {/* ---- 窄屏上下文抽屉（<xl 由头部按钮拉出） ---- */}
      <AnimatePresence>
        {contextOpen && (
          <motion.div
            className="fixed inset-0 z-[80] xl:hidden"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
          >
            <div
              className="absolute inset-0 bg-base/70 backdrop-blur-sm"
              onClick={() => setContextOpen(false)}
              aria-hidden
            />
            <motion.aside
              role="dialog"
              aria-modal="true"
              aria-label="上下文面板"
              initial={{ x: "100%" }}
              animate={{ x: 0 }}
              exit={{ x: "100%" }}
              transition={{ type: "spring", stiffness: 380, damping: 34 }}
              style={{ overscrollBehavior: "contain" }}
              className="absolute right-0 top-0 flex h-full w-80 max-w-[85vw] flex-col border-l border-line/8 bg-elevated/95 p-4 shadow-panel-lg backdrop-blur-xl"
            >
              <div className="mb-2 flex shrink-0 items-center justify-between">
                <span className="font-display text-sm font-semibold text-primary">上下文</span>
                <button
                  type="button"
                  onClick={() => setContextOpen(false)}
                  aria-label="关闭上下文面板"
                  className="rounded-md p-1.5 text-muted transition-colors hover:bg-line/8 hover:text-primary"
                >
                  <X className="size-4" />
                </button>
              </div>
              <div className="min-h-0 flex-1">
                <ContextPanel />
              </div>
            </motion.aside>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
