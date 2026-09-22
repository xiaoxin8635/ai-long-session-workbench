/**
 * 任务面板页（EchoDesk 前端 · 「宣纸书卷」古风，M-F5）。
 *
 * 任务清单（状态过滤）+ 创建 + 状态流转（open/doing/done/abandoned）+
 * 优先级切换。与 Agent 的 todo 工具共用后端 TaskService，状态变更
 * 自动同步 job.*.progress 记忆（对话开场简报注入）。
 */
import { useEffect, useState, type JSX } from "react";
import { Star } from "lucide-react";
import type { Task, TaskStatus } from "../api/tasks";
import { useTasksStore } from "../stores/tasks";

/** 状态徽标文案与样式（古风 token 口径）。 */
const STATUS_LABELS: Record<TaskStatus, string> = {
  open: "待开始",
  doing: "进行中",
  done: "已完成",
  abandoned: "已放弃",
};
const STATUS_STYLES: Record<TaskStatus, string> = {
  open: "bg-info/12 text-info ring-1 ring-inset ring-info/30",
  doing: "bg-warning/14 text-warning ring-1 ring-inset ring-warning/30",
  done: "bg-success/14 text-success ring-1 ring-inset ring-success/30",
  abandoned: "bg-line/6 text-secondary ring-1 ring-inset ring-line/12",
};

/** 状态过滤项（null = 全部）。 */
const FILTERS: Array<{ value: TaskStatus | null; label: string }> = [
  { value: null, label: "全部" },
  { value: "open", label: "待开始" },
  { value: "doing", label: "进行中" },
  { value: "done", label: "已完成" },
  { value: "abandoned", label: "已放弃" },
];

/** 各状态下可流转的目标动作。 */
const TRANSITIONS: Record<TaskStatus, Array<{ target: TaskStatus; label: string }>> = {
  open: [
    { target: "doing", label: "开始" },
    { target: "done", label: "完成" },
    { target: "abandoned", label: "放弃" },
  ],
  doing: [
    { target: "done", label: "完成" },
    { target: "open", label: "重开" },
    { target: "abandoned", label: "放弃" },
  ],
  done: [{ target: "open", label: "重开" }],
  abandoned: [{ target: "open", label: "重开" }],
};

/**
 * 任务面板页组件。
 *
 * @returns 页面 JSX。
 */
export default function TasksPage(): JSX.Element {
  const tasks = useTasksStore((s) => s.tasks);
  const filterStatus = useTasksStore((s) => s.filterStatus);
  const creating = useTasksStore((s) => s.creating);
  const error = useTasksStore((s) => s.error);
  const bootstrap = useTasksStore((s) => s.bootstrap);
  const setFilter = useTasksStore((s) => s.setFilter);
  const create = useTasksStore((s) => s.create);
  const updateStatus = useTasksStore((s) => s.updateStatus);
  const setPriority = useTasksStore((s) => s.setPriority);
  const clearError = useTasksStore((s) => s.clearError);

  /** 新任务标题草稿。 */
  const [titleDraft, setTitleDraft] = useState("");
  /** 新任务高优先级勾选。 */
  const [highPriority, setHighPriority] = useState(false);

  // 首次进入：取 workspace + 任务列表
  useEffect(() => {
    void bootstrap().then(() => {
      void useTasksStore.getState().fetchTasks();
    });
  }, [bootstrap]);

  /**
   * 提交创建（成功后清空草稿）。
   */
  async function submitCreate(): Promise<void> {
    const ok = await create(titleDraft, highPriority ? 1 : 0);
    if (ok) {
      setTitleDraft("");
      setHighPriority(false);
    }
  }

  return (
    <div className="anim-rise-in relative mx-auto max-w-3xl space-y-5 p-6">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 font-display text-lg font-semibold text-primary">
          <span className="bookmark-bar h-4" aria-hidden />
          任务
        </h1>
        <span className="font-mono text-xs text-muted">
          {filterStatus === null ? `共 ${tasks.length} 条` : `本视图 ${tasks.length} 条`}
        </span>
      </div>

      {error !== null && (
        <div
          role="alert"
          className="flex items-center justify-between rounded-lg border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger"
        >
          <span className="truncate">{error}</span>
          <button type="button" onClick={clearError} className="ml-3 text-danger" aria-label="关闭错误提示">
            ✕
          </button>
        </div>
      )}

      {/* 创建行 */}
      <div className="flex items-center gap-2">
        <input
          value={titleDraft}
          onChange={(e) => setTitleDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              void submitCreate();
            }
          }}
          placeholder="新任务标题，Enter 创建"
          className="input min-w-0 flex-1 rounded-lg px-3 py-2 text-sm"
        />
        <label className="flex shrink-0 cursor-pointer items-center gap-1.5 text-xs text-secondary">
          <input
            type="checkbox"
            checked={highPriority}
            onChange={(e) => setHighPriority(e.target.checked)}
            className="size-3.5 accent-[rgb(var(--c-accent))]"
          />
          高优（开场注入）
        </label>
        <button
          type="button"
          disabled={creating || titleDraft.trim() === ""}
          onClick={() => void submitCreate()}
          className="btn-primary shrink-0 rounded-lg px-3 py-2 text-sm"
        >
          创建
        </button>
      </div>

      {/* 状态过滤 */}
      <div className="flex flex-wrap gap-1.5">
        {FILTERS.map((f) => (
          <button
            key={f.label}
            type="button"
            onClick={() => void setFilter(f.value)}
            className={
              "rounded-lg px-3 py-1 text-xs transition-colors " +
              (filterStatus === f.value
                ? "btn-primary"
                : "btn-ghost")
            }
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* 任务列表 */}
      <div className="space-y-2">
        {tasks.map((t, i) => (
          <TaskCard
            key={t.id}
            task={t}
            index={i}
            onTransition={(target) => void updateStatus(t.id, target)}
            onTogglePriority={() => void setPriority(t.id, t.priority === 1 ? 0 : 1)}
          />
        ))}
        {tasks.length === 0 && (
          <p className="py-10 text-center font-display text-sm text-muted">
            暂无任务 · 对话中也可以让助手用 todo 工具代建
          </p>
        )}
      </div>
    </div>
  );
}

/**
 * 任务卡片。
 *
 * @param props - task 任务；index 序号（入场 stagger）；onTransition 状态流转回调；
 *   onTogglePriority 优先级切换回调。
 * @returns 卡片 JSX。
 */
function TaskCard({
  task,
  index,
  onTransition,
  onTogglePriority,
}: {
  task: Task;
  index: number;
  onTransition: (target: TaskStatus) => void;
  onTogglePriority: () => void;
}): JSX.Element {
  const highPriority = task.priority === 1;
  return (
    <div
      className="card-paper anim-rise-in rounded-xl p-3"
      style={{ animationDelay: `${Math.min(index, 12) * 30}ms` }}
    >
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={onTogglePriority}
          title={highPriority ? "取消高优" : "设为高优（对话开场注入）"}
          className={
            "rounded-md p-0.5 transition-colors " +
            (highPriority ? "text-warning" : "text-muted hover:text-warning")
          }
          aria-label="切换优先级"
        >
          <Star className="size-4" strokeWidth={2} fill={highPriority ? "currentColor" : "none"} />
        </button>
        <span
          className={
            "flex-1 text-sm " +
            (task.status === "done" || task.status === "abandoned"
              ? "text-muted line-through"
              : "text-primary")
          }
        >
          {task.title}
        </span>
        <span className={"rounded-full px-2 py-0.5 text-[10px] font-medium " + STATUS_STYLES[task.status]}>
          {STATUS_LABELS[task.status]}
        </span>
      </div>
      <div className="mt-2 flex items-center gap-2 font-mono text-[10px] text-muted">
        {task.due_date !== null && <span>截止 {new Date(task.due_date).toLocaleDateString()}</span>}
        {task.related_session_ids.length > 0 && <span>关联会话 {task.related_session_ids.length}</span>}
        <span className="ml-auto flex gap-1">
          {TRANSITIONS[task.status].map((t) => (
            <button
              key={t.target}
              type="button"
              onClick={() => onTransition(t.target)}
              className="rounded-md bg-line/6 px-2 py-0.5 text-[10px] text-secondary transition-colors hover:bg-accent/12 hover:text-accent"
            >
              {t.label}
            </button>
          ))}
        </span>
      </div>
    </div>
  );
}
