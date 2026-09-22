/**
 * 记忆工作台页（EchoDesk 前端 · 「宣纸书卷」古风，M-F4）。
 *
 * 四层记忆的可视化管理：过滤列表（conflicted 置顶提示）、条目详情
 * （编辑/撤销式软删除/版本链/事件流水）、冲突裁决（保留本条/对手方）、
 * 检索测试（与对话链路同检索器）。
 */
import { useEffect, useRef, useState, type JSX } from "react";
import { Search, X } from "lucide-react";
import type { MemoryItem, MemoryStatus, MemoryType } from "../api/memories";
import { useMemoriesStore } from "../stores/memories";
import { showToast } from "../stores/toast";

/** 类型/状态徽标文案与样式（古风 token 口径）。 */
const TYPE_LABELS: Record<MemoryType, string> = {
  semantic: "语义",
  procedural: "偏好",
  episodic: "情节",
};
const TYPE_STYLES: Record<MemoryType, string> = {
  semantic: "bg-info/12 text-info ring-1 ring-inset ring-info/30",
  procedural: "bg-violet/12 text-violet ring-1 ring-inset ring-violet/30",
  episodic: "bg-teal/12 text-teal ring-1 ring-inset ring-teal/30",
};
const STATUS_LABELS: Record<MemoryStatus, string> = {
  active: "生效",
  conflicted: "冲突待裁决",
  superseded: "已替代",
  archived: "已归档",
  deleted: "已删除",
};
const STATUS_STYLES: Record<MemoryStatus, string> = {
  active: "bg-success/14 text-success ring-1 ring-inset ring-success/30",
  conflicted: "bg-warning/14 text-warning ring-1 ring-inset ring-warning/30",
  superseded: "bg-line/6 text-secondary ring-1 ring-inset ring-line/12",
  archived: "bg-line/6 text-secondary ring-1 ring-inset ring-line/12",
  deleted: "bg-danger/12 text-danger ring-1 ring-inset ring-danger/30",
};

/**
 * 记忆工作台组件。
 *
 * @returns 页面 JSX。
 */
export default function MemoriesPage(): JSX.Element {
  const items = useMemoriesStore((s) => s.items);
  const total = useMemoriesStore((s) => s.total);
  const offset = useMemoriesStore((s) => s.offset);
  const filterType = useMemoriesStore((s) => s.filterType);
  const filterStatus = useMemoriesStore((s) => s.filterStatus);
  const detail = useMemoriesStore((s) => s.detail);
  const searchHits = useMemoriesStore((s) => s.searchHits);
  const error = useMemoriesStore((s) => s.error);
  const bootstrap = useMemoriesStore((s) => s.bootstrap);
  const fetchPage = useMemoriesStore((s) => s.fetchPage);
  const setFilters = useMemoriesStore((s) => s.setFilters);
  const loadDetail = useMemoriesStore((s) => s.loadDetail);
  const closeDetail = useMemoriesStore((s) => s.closeDetail);
  const removeMemory = useMemoriesStore((s) => s.removeMemory);
  const resolveMemory = useMemoriesStore((s) => s.resolveMemory);
  const search = useMemoriesStore((s) => s.search);
  const clearError = useMemoriesStore((s) => s.clearError);

  /** 关键词输入框草稿（提交时才进 store 触发拉取）。 */
  const [queryDraft, setQueryDraft] = useState("");
  /** 检索测试输入。 */
  const [searchDraft, setSearchDraft] = useState("");
  /** 延迟删除中的记忆 ID（乐观隐藏 + 撤销窗口；null = 无）。 */
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  /** 延迟删除计时器（撤销时清除）。 */
  const deleteTimerRef = useRef<number | null>(null);

  // 首次进入：拉 workspace + 第一页
  useEffect(() => {
    void bootstrap().then(() => {
      void useMemoriesStore.getState().fetchPage(0);
    });
  }, [bootstrap]);

  // 卸载时清理延迟删除计时器
  useEffect(() => {
    return () => {
      if (deleteTimerRef.current !== null) {
        window.clearTimeout(deleteTimerRef.current);
      }
    };
  }, []);

  /**
   * 软删除记忆（乐观隐藏 + 5s 撤销窗口；窗口过后才真正调用删除 API）。
   *
   * @param item - 待删除记忆条目。
   */
  function handleDelete(item: MemoryItem): void {
    closeDetail();
    setPendingDeleteId(item.id);
    deleteTimerRef.current = window.setTimeout(() => {
      deleteTimerRef.current = null;
      setPendingDeleteId(null);
      void removeMemory(item.id);
    }, 5000);
    showToast({
      message: `已删除记忆「${item.key}」`,
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
        },
      },
    });
  }

  /** 冲突条目数（>0 时顶部提示条）。 */
  const conflictCount = items.filter((m) => m.status === "conflicted").length;

  return (
    <div className="anim-rise-in relative mx-auto max-w-4xl space-y-5 p-6">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 font-display text-lg font-semibold text-primary">
          <span className="bookmark-bar h-4" aria-hidden />
          记忆工作台
        </h1>
        <span className="font-mono text-xs text-muted">共 {total} 条</span>
      </div>

      {error !== null && (
        <div
          role="alert"
          className="flex items-center justify-between rounded-lg border border-danger/25 bg-danger/10 px-3 py-2 text-xs text-danger"
        >
          <span className="truncate">{error}</span>
          <button type="button" onClick={clearError} className="ml-3 text-danger" aria-label="关闭错误提示">
            <X className="size-3.5" strokeWidth={2.2} />
          </button>
        </div>
      )}

      {conflictCount > 0 && (
        <div className="rounded-lg border border-warning/30 bg-warning/10 px-3 py-2 text-xs text-warning">
          本页有 {conflictCount} 条冲突待裁决（置顶展示），请打开条目进行裁决
        </div>
      )}

      {/* 过滤条 */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={filterType ?? ""}
          onChange={(e) => setFilters({ type: (e.target.value || null) as MemoryType | null })}
          className="input rounded-lg px-2.5 py-1.5 text-xs"
        >
          <option value="">全部类型</option>
          <option value="semantic">语义</option>
          <option value="procedural">偏好</option>
          <option value="episodic">情节</option>
        </select>
        <select
          value={filterStatus ?? ""}
          onChange={(e) => setFilters({ status: (e.target.value || null) as MemoryStatus | null })}
          className="input rounded-lg px-2.5 py-1.5 text-xs"
        >
          <option value="">全部状态</option>
          <option value="active">生效</option>
          <option value="conflicted">冲突</option>
          <option value="superseded">已替代</option>
          <option value="archived">已归档</option>
        </select>
        <input
          value={queryDraft}
          onChange={(e) => setQueryDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              setFilters({ q: queryDraft });
            }
          }}
          placeholder="key/content 关键词"
          className="input w-48 rounded-lg px-2.5 py-1.5 text-xs"
        />
        <button
          type="button"
          onClick={() => setFilters({ q: queryDraft })}
          className="btn-ghost rounded-lg px-3 py-1.5 text-xs font-medium"
        >
          筛选
        </button>
      </div>

      {/* 列表 */}
      <div className="space-y-2">
        {items
          .filter((m) => m.id !== pendingDeleteId)
          .map((m, i) => (
            <MemoryCard key={m.id} item={m} index={i} onOpen={() => void loadDetail(m.id)} />
          ))}
        {items.length === 0 && (
          <p className="py-10 text-center font-display text-sm text-muted">
            尚无记忆 · 对话中陈述的事实会自动沉淀至此
          </p>
        )}
      </div>

      {/* 分页 */}
      {total > 20 && (
        <div className="flex items-center justify-center gap-3 text-xs text-secondary">
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => void fetchPage(Math.max(0, offset - 20))}
            className="btn-ghost rounded-lg px-3 py-1 disabled:opacity-40"
          >
            上一页
          </button>
          <span className="font-mono">
            {offset + 1}-{Math.min(offset + 20, total)} / {total}
          </span>
          <button
            type="button"
            disabled={offset + 20 >= total}
            onClick={() => void fetchPage(offset + 20)}
            className="btn-ghost rounded-lg px-3 py-1 disabled:opacity-40"
          >
            下一页
          </button>
        </div>
      )}

      {/* 检索测试 */}
      <section className="rounded-xl border border-line/12 bg-surface/50 p-4 shadow-panel">
        <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          检索测试（与对话链路同检索器）
        </h2>
        <div className="mt-3 flex gap-2">
          <input
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                void search(searchDraft);
              }
            }}
            placeholder="输入模拟 query，验证能召回哪些记忆"
            className="input min-w-0 flex-1 rounded-lg px-2.5 py-1.5 text-xs"
          />
          <button
            type="button"
            onClick={() => void search(searchDraft)}
            className="btn-primary inline-flex shrink-0 items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs"
          >
            <Search className="size-3.5" strokeWidth={2.2} aria-hidden />
            检索
          </button>
        </div>
        {searchHits.length > 0 && (
          <ul className="mt-3 space-y-1.5">
            {searchHits.map((h) => (
              <li
                key={h.id}
                className="rounded-lg border border-line/10 bg-elevated/70 px-3 py-2 text-xs"
              >
                <div className="flex items-center gap-2">
                  <span className={"rounded-full px-2 py-0.5 text-[10px] font-medium " + TYPE_STYLES[h.memory_type]}>
                    {TYPE_LABELS[h.memory_type] ?? h.memory_type}
                  </span>
                  <code className="font-mono font-medium text-primary">{h.key}</code>
                  <span className="ml-auto font-mono text-muted">
                    score {h.score.toFixed(3)} · sim {h.similarity.toFixed(3)}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-secondary">{h.content}</p>
              </li>
            ))}
          </ul>
        )}
        {searchHits.length === 0 && <p className="mt-2 text-xs text-muted">尚无检索结果</p>}
      </section>

      {/* 详情抽屉 */}
      {detail !== null && (
        <MemoryDetailPanel
          onResolve={(keep) => void resolveMemory(detail.id, keep)}
          onDelete={() => handleDelete(detail)}
          onClose={closeDetail}
        />
      )}
    </div>
  );
}

/**
 * 记忆列表卡片。
 *
 * @param props - item 条目；index 序号（入场 stagger）；onOpen 打开详情回调。
 * @returns 卡片 JSX。
 */
function MemoryCard({
  item,
  index,
  onOpen,
}: {
  item: MemoryItem;
  index: number;
  onOpen: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onOpen}
      style={{ animationDelay: `${Math.min(index, 12) * 30}ms` }}
      className="card-paper anim-rise-in w-full rounded-xl p-3 text-left"
    >
      <div className="flex items-center gap-2">
        <span className={"rounded-full px-2 py-0.5 text-[10px] font-medium " + TYPE_STYLES[item.memory_type]}>
          {TYPE_LABELS[item.memory_type] ?? item.memory_type}
        </span>
        <span className={"rounded-full px-2 py-0.5 text-[10px] font-medium " + STATUS_STYLES[item.status]}>
          {STATUS_LABELS[item.status] ?? item.status}
        </span>
        <code className="truncate font-mono text-xs font-medium text-primary">{item.key}</code>
        <span className="ml-auto shrink-0 font-mono text-[10px] text-muted">
          v{item.version} · 命中 {item.hit_count}
        </span>
      </div>
      <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-secondary">
        {item.content}
      </p>
    </button>
  );
}

/**
 * 详情抽屉（编辑/裁决/撤销式删除 + 版本链 + 事件流水）。
 *
 * 直接读写 memories store 的 detail，减少 props 透传。
 *
 * @param props - onResolve 裁决回调；onDelete 删除回调；onClose 关闭回调。
 * @returns 抽屉 JSX。
 */
function MemoryDetailPanel({
  onResolve,
  onDelete,
  onClose,
}: {
  onResolve: (keep: "this" | "other") => void;
  onDelete: () => void;
  onClose: () => void;
}): JSX.Element {
  const detail = useMemoriesStore((s) => s.detail)!;
  const editMemory = useMemoriesStore((s) => s.editMemory);

  /** 内容编辑草稿。 */
  const [contentDraft, setContentDraft] = useState(detail.content);
  /** 置信度草稿。 */
  const [confidenceDraft, setConfidenceDraft] = useState(String(detail.confidence));
  /** 重要度草稿。 */
  const [importanceDraft, setImportanceDraft] = useState(String(detail.importance));
  /** 保存中标记。 */
  const [saving, setSaving] = useState(false);
  /** 展开版本链/事件流水。 */
  const [showHistory, setShowHistory] = useState(false);

  /**
   * 提交编辑（空值/越界校验后调 store）。
   */
  async function save(): Promise<void> {
    const confidence = Number(confidenceDraft);
    const importance = Number(importanceDraft);
    if (contentDraft.trim() === "" || Number.isNaN(confidence) || Number.isNaN(importance)) {
      return;
    }
    setSaving(true);
    await editMemory(detail.id, {
      content: contentDraft.trim(),
      confidence: Math.min(1, Math.max(0, confidence)),
      importance: Math.min(1, Math.max(0, importance)),
    });
    setSaving(false);
  }

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-primary/25 backdrop-blur-sm" onClick={onClose}>
      <div
        className="anim-ink-diffuse h-full w-full max-w-lg overflow-y-auto border-l border-line/12 bg-surface p-5 shadow-panel-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <span className={"rounded-full px-2 py-0.5 text-[10px] font-medium " + TYPE_STYLES[detail.memory_type]}>
            {TYPE_LABELS[detail.memory_type] ?? detail.memory_type}
          </span>
          <span className={"rounded-full px-2 py-0.5 text-[10px] font-medium " + STATUS_STYLES[detail.status]}>
            {STATUS_LABELS[detail.status] ?? detail.status}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto rounded-md p-1 text-muted transition-colors hover:bg-line/8 hover:text-primary"
            aria-label="关闭详情"
          >
            <X className="size-4" strokeWidth={2.2} />
          </button>
        </div>
        <code className="mt-2 block font-mono text-xs text-muted">{detail.key}</code>

        {/* 编辑区 */}
        <textarea
          value={contentDraft}
          onChange={(e) => setContentDraft(e.target.value)}
          rows={4}
          className="input mt-3 w-full rounded-lg px-2.5 py-2 text-sm leading-6"
        />
        <div className="mt-2 flex items-center gap-3 text-xs">
          <label className="flex items-center gap-1.5 text-secondary">
            置信度
            <input
              value={confidenceDraft}
              onChange={(e) => setConfidenceDraft(e.target.value)}
              className="input w-16 rounded px-1.5 py-1 font-mono"
            />
          </label>
          <label className="flex items-center gap-1.5 text-secondary">
            重要度
            <input
              value={importanceDraft}
              onChange={(e) => setImportanceDraft(e.target.value)}
              className="input w-16 rounded px-1.5 py-1 font-mono"
            />
          </label>
          <button
            type="button"
            disabled={saving}
            onClick={() => void save()}
            className="btn-primary ml-auto rounded-lg px-3 py-1.5"
          >
            保存
          </button>
        </div>

        {/* 冲突裁决 */}
        {detail.status === "conflicted" && (
          <div className="mt-4 rounded-xl border border-warning/30 bg-warning/10 p-3 text-xs">
            <p className="font-display font-semibold text-warning">冲突待裁决</p>
            <p className="mt-1 text-secondary">
              同 key 存在相互矛盾的两条记忆：保留当前条目，或保留检索到的对手方（对方将挂入版本链）。
            </p>
            <div className="mt-2.5 flex gap-2">
              <button
                type="button"
                onClick={() => onResolve("this")}
                className="btn-primary rounded-lg px-3 py-1.5"
              >
                保留本条
              </button>
              <button
                type="button"
                onClick={() => onResolve("other")}
                className="btn-ghost rounded-lg px-3 py-1.5"
              >
                保留对手方
              </button>
            </div>
          </div>
        )}

        {/* 删除 + 历史 */}
        <div className="mt-4 flex items-center gap-3 text-xs">
          <button
            type="button"
            onClick={onDelete}
            className="rounded-lg px-2 py-1 text-danger transition-colors hover:bg-danger/12"
          >
            软删除
          </button>
          <button
            type="button"
            onClick={() => setShowHistory((v) => !v)}
            className="text-accent transition-colors hover:text-accent-bright"
          >
            {showHistory ? "收起历史" : `版本链 ${detail.version_chain.length} · 事件 ${detail.events.length}`}
          </button>
        </div>

        {showHistory && (
          <div className="mt-3 space-y-3 text-xs">
            <div>
              <p className="mb-1 flex items-center gap-1.5 font-display font-medium text-secondary">
                <span className="bookmark-bar h-3" aria-hidden />
                版本链（时间倒序）
              </p>
              {detail.version_chain.length === 0 && <p className="text-muted">无被替代版本</p>}
              {detail.version_chain.map((v) => (
                <div key={v.id} className="mt-1 rounded-lg border border-line/10 bg-elevated/70 p-2">
                  <span className="font-mono text-muted">v{v.version}</span>
                  <p className="mt-0.5 line-clamp-2 text-secondary">{v.content}</p>
                </div>
              ))}
            </div>
            <div>
              <p className="mb-1 flex items-center gap-1.5 font-display font-medium text-secondary">
                <span className="bookmark-bar h-3" aria-hidden />
                事件流水（最近 20 条）
              </p>
              {detail.events.length === 0 && <p className="text-muted">无事件</p>}
              <ul className="space-y-1">
                {detail.events.map((ev, i) => (
                  <li key={i} className="flex gap-2 text-muted">
                    <span className="shrink-0 font-mono">{new Date(ev.created_at).toLocaleString()}</span>
                    <span className="rounded bg-line/6 px-1 text-secondary">{ev.event_type}</span>
                    <span className="truncate" title={ev.new_value ?? ev.old_value ?? ""}>
                      {ev.new_value ?? ev.old_value ?? "-"}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
