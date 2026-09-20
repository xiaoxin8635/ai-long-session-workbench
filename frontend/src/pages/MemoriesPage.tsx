/**
 * 记忆工作台页（EchoDesk 前端，M-F4）。
 *
 * 四层记忆的可视化管理：过滤列表（conflicted 置顶提示）、条目详情
 * （编辑/软删除/版本链/事件流水）、冲突裁决（保留本条/对手方）、
 * 检索测试（与对话链路同检索器）。
 */
import { useEffect, useState, type JSX } from "react";
import type { MemoryItem, MemoryStatus, MemoryType } from "../api/memories";
import { useMemoriesStore } from "../stores/memories";

/** 类型/状态徽标文案与样式。 */
const TYPE_LABELS: Record<MemoryType, string> = {
  semantic: "语义",
  procedural: "偏好",
  episodic: "情节",
};
const TYPE_STYLES: Record<MemoryType, string> = {
  semantic: "bg-sky-100 text-sky-700 dark:bg-sky-950 dark:text-sky-400",
  procedural: "bg-violet-100 text-violet-700 dark:bg-violet-950 dark:text-violet-400",
  episodic: "bg-teal-100 text-teal-700 dark:bg-teal-950 dark:text-teal-400",
};
const STATUS_LABELS: Record<MemoryStatus, string> = {
  active: "生效",
  conflicted: "冲突待裁决",
  superseded: "已替代",
  archived: "已归档",
  deleted: "已删除",
};
const STATUS_STYLES: Record<MemoryStatus, string> = {
  active: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400",
  conflicted: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-400",
  superseded: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
  archived: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
  deleted: "bg-red-100 text-red-600 dark:bg-red-950 dark:text-red-400",
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

  // 首次进入：拉 workspace + 第一页
  useEffect(() => {
    void bootstrap().then(() => {
      void useMemoriesStore.getState().fetchPage(0);
    });
  }, [bootstrap]);

  /**
   * 冲突条目数（>0 时顶部提示条）。
   */
  const conflictCount = items.filter((m) => m.status === "conflicted").length;

  return (
    <div className="relative mx-auto max-w-4xl space-y-5 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">记忆工作台</h1>
        <span className="text-xs text-slate-400">共 {total} 条</span>
      </div>

      {error !== null && (
        <div
          role="alert"
          className="flex items-center justify-between rounded-lg bg-red-50 px-3 py-2 text-xs text-red-700 dark:bg-red-950 dark:text-red-300"
        >
          <span className="truncate">{error}</span>
          <button type="button" onClick={clearError} className="ml-3 text-red-500" aria-label="关闭错误提示">
            ✕
          </button>
        </div>
      )}

      {conflictCount > 0 && (
        <div className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-700 dark:bg-amber-950 dark:text-amber-300">
          本页有 {conflictCount} 条冲突待裁决（置顶展示），请打开条目进行裁决
        </div>
      )}

      {/* 过滤条 */}
      <div className="flex flex-wrap items-center gap-2">
        <select
          value={filterType ?? ""}
          onChange={(e) => setFilters({ type: (e.target.value || null) as MemoryType | null })}
          className="rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
        >
          <option value="">全部类型</option>
          <option value="semantic">语义</option>
          <option value="procedural">偏好</option>
          <option value="episodic">情节</option>
        </select>
        <select
          value={filterStatus ?? ""}
          onChange={(e) => setFilters({ status: (e.target.value || null) as MemoryStatus | null })}
          className="rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
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
          className="w-48 rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
        />
        <button
          type="button"
          onClick={() => setFilters({ q: queryDraft })}
          className="rounded-lg bg-slate-200 px-3 py-1.5 text-xs font-medium transition hover:bg-slate-300 dark:bg-slate-800 dark:hover:bg-slate-700"
        >
          筛选
        </button>
      </div>

      {/* 列表 */}
      <div className="space-y-2">
        {items.map((m) => (
          <MemoryCard key={m.id} item={m} onOpen={() => void loadDetail(m.id)} />
        ))}
        {items.length === 0 && (
          <p className="py-8 text-center text-xs text-slate-400">暂无记忆（对话中陈述的事实会自动沉淀至此）</p>
        )}
      </div>

      {/* 分页 */}
      {total > 20 && (
        <div className="flex items-center justify-center gap-3 text-xs text-slate-500">
          <button
            type="button"
            disabled={offset === 0}
            onClick={() => void fetchPage(Math.max(0, offset - 20))}
            className="rounded-lg bg-slate-200 px-3 py-1 transition hover:bg-slate-300 disabled:opacity-40 dark:bg-slate-800"
          >
            上一页
          </button>
          <span>
            {offset + 1}-{Math.min(offset + 20, total)} / {total}
          </span>
          <button
            type="button"
            disabled={offset + 20 >= total}
            onClick={() => void fetchPage(offset + 20)}
            className="rounded-lg bg-slate-200 px-3 py-1 transition hover:bg-slate-300 disabled:opacity-40 dark:bg-slate-800"
          >
            下一页
          </button>
        </div>
      )}

      {/* 检索测试 */}
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="text-sm font-semibold">检索测试（与对话链路同检索器）</h2>
        <div className="mt-2 flex gap-2">
          <input
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                void search(searchDraft);
              }
            }}
            placeholder="输入模拟 query，验证能召回哪些记忆"
            className="flex-1 rounded-lg border border-slate-300 px-2 py-1.5 text-xs dark:border-slate-700 dark:bg-slate-900"
          />
          <button
            type="button"
            onClick={() => void search(searchDraft)}
            className="rounded-lg bg-sky-600 px-3 py-1.5 text-xs font-medium text-white transition hover:bg-sky-700"
          >
            检索
          </button>
        </div>
        {searchHits.length > 0 && (
          <ul className="mt-3 space-y-1.5">
            {searchHits.map((h) => (
              <li
                key={h.id}
                className="rounded-lg bg-slate-50 px-3 py-2 text-xs dark:bg-slate-900"
              >
                <div className="flex items-center gap-2">
                  <span className={"rounded px-1.5 py-0.5 text-[10px] " + TYPE_STYLES[h.memory_type]}>
                    {TYPE_LABELS[h.memory_type] ?? h.memory_type}
                  </span>
                  <code className="font-medium">{h.key}</code>
                  <span className="ml-auto text-slate-400">
                    score {h.score.toFixed(3)} · sim {h.similarity.toFixed(3)}
                  </span>
                </div>
                <p className="mt-1 line-clamp-2 text-slate-600 dark:text-slate-300">{h.content}</p>
              </li>
            ))}
          </ul>
        )}
        {searchHits.length === 0 && <p className="mt-2 text-xs text-slate-400">尚无检索结果</p>}
      </section>

      {/* 详情抽屉 */}
      {detail !== null && (
        <MemoryDetailPanel
          onResolve={(keep) => void resolveMemory(detail.id, keep)}
          onDelete={() => {
            if (window.confirm("软删除该记忆？列表与检索将不再可见。")) {
              void removeMemory(detail.id);
            }
          }}
          onClose={closeDetail}
        />
      )}
    </div>
  );
}

/**
 * 记忆列表卡片。
 *
 * @param props - item 条目；onOpen 打开详情回调。
 * @returns 卡片 JSX。
 */
function MemoryCard({ item, onOpen }: { item: MemoryItem; onOpen: () => void }): JSX.Element {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="w-full rounded-xl border border-slate-200 bg-white p-3 text-left transition
                 hover:border-sky-400 dark:border-slate-800 dark:bg-slate-900 dark:hover:border-sky-600"
    >
      <div className="flex items-center gap-2">
        <span className={"rounded px-1.5 py-0.5 text-[10px] " + TYPE_STYLES[item.memory_type]}>
          {TYPE_LABELS[item.memory_type] ?? item.memory_type}
        </span>
        <span className={"rounded px-1.5 py-0.5 text-[10px] " + STATUS_STYLES[item.status]}>
          {STATUS_LABELS[item.status] ?? item.status}
        </span>
        <code className="truncate text-xs font-medium">{item.key}</code>
        <span className="ml-auto shrink-0 text-[10px] text-slate-400">
          v{item.version} · 命中 {item.hit_count}
        </span>
      </div>
      <p className="mt-1.5 line-clamp-2 text-xs leading-5 text-slate-600 dark:text-slate-300">
        {item.content}
      </p>
    </button>
  );
}

/**
 * 详情抽屉（编辑/裁决/删除 + 版本链 + 事件流水）。
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
    <div className="fixed inset-0 z-40 flex justify-end bg-black/30" onClick={onClose}>
      <div
        className="h-full w-full max-w-lg overflow-y-auto bg-white p-5 shadow-xl dark:bg-slate-900"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2">
          <span className={"rounded px-1.5 py-0.5 text-[10px] " + TYPE_STYLES[detail.memory_type]}>
            {TYPE_LABELS[detail.memory_type] ?? detail.memory_type}
          </span>
          <span className={"rounded px-1.5 py-0.5 text-[10px] " + STATUS_STYLES[detail.status]}>
            {STATUS_LABELS[detail.status] ?? detail.status}
          </span>
          <button
            type="button"
            onClick={onClose}
            className="ml-auto text-slate-400 hover:text-slate-600"
            aria-label="关闭详情"
          >
            ✕
          </button>
        </div>
        <code className="mt-2 block text-xs text-slate-500">{detail.key}</code>

        {/* 编辑区 */}
        <textarea
          value={contentDraft}
          onChange={(e) => setContentDraft(e.target.value)}
          rows={4}
          className="mt-3 w-full rounded-lg border border-slate-300 px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-800"
        />
        <div className="mt-2 flex gap-3 text-xs">
          <label className="flex items-center gap-1">
            置信度
            <input
              value={confidenceDraft}
              onChange={(e) => setConfidenceDraft(e.target.value)}
              className="w-16 rounded border border-slate-300 px-1.5 py-1 dark:border-slate-700 dark:bg-slate-800"
            />
          </label>
          <label className="flex items-center gap-1">
            重要度
            <input
              value={importanceDraft}
              onChange={(e) => setImportanceDraft(e.target.value)}
              className="w-16 rounded border border-slate-300 px-1.5 py-1 dark:border-slate-700 dark:bg-slate-800"
            />
          </label>
          <button
            type="button"
            disabled={saving}
            onClick={() => void save()}
            className="ml-auto rounded-lg bg-sky-600 px-3 py-1 font-medium text-white transition hover:bg-sky-700 disabled:opacity-50"
          >
            保存
          </button>
        </div>

        {/* 冲突裁决 */}
        {detail.status === "conflicted" && (
          <div className="mt-4 rounded-xl border border-amber-300 bg-amber-50 p-3 text-xs dark:border-amber-700 dark:bg-amber-950">
            <p className="font-medium text-amber-700 dark:text-amber-300">冲突待裁决</p>
            <p className="mt-1 text-amber-600 dark:text-amber-400">
              同 key 存在相互矛盾的两条记忆：保留当前条目，或保留检索到的对手方（对方将挂入版本链）。
            </p>
            <div className="mt-2 flex gap-2">
              <button
                type="button"
                onClick={() => onResolve("this")}
                className="rounded-lg bg-emerald-600 px-3 py-1.5 font-medium text-white transition hover:bg-emerald-700"
              >
                保留本条
              </button>
              <button
                type="button"
                onClick={() => onResolve("other")}
                className="rounded-lg bg-slate-200 px-3 py-1.5 font-medium text-slate-700 transition hover:bg-slate-300 dark:bg-slate-800 dark:text-slate-300"
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
            className="text-red-500 transition hover:text-red-600"
          >
            软删除
          </button>
          <button
            type="button"
            onClick={() => setShowHistory((v) => !v)}
            className="text-sky-600 hover:underline dark:text-sky-400"
          >
            {showHistory ? "收起历史" : `版本链 ${detail.version_chain.length} · 事件 ${detail.events.length}`}
          </button>
        </div>

        {showHistory && (
          <div className="mt-3 space-y-3 text-xs">
            <div>
              <p className="mb-1 font-medium text-slate-500">版本链（时间倒序）</p>
              {detail.version_chain.length === 0 && <p className="text-slate-400">无被替代版本</p>}
              {detail.version_chain.map((v) => (
                <div key={v.id} className="rounded-lg bg-slate-50 p-2 dark:bg-slate-800">
                  <span className="text-slate-400">v{v.version}</span>
                  <p className="mt-0.5 line-clamp-2 text-slate-600 dark:text-slate-300">{v.content}</p>
                </div>
              ))}
            </div>
            <div>
              <p className="mb-1 font-medium text-slate-500">事件流水（最近 20 条）</p>
              {detail.events.length === 0 && <p className="text-slate-400">无事件</p>}
              <ul className="space-y-1">
                {detail.events.map((ev, i) => (
                  <li key={i} className="flex gap-2 text-slate-500 dark:text-slate-400">
                    <span className="shrink-0">{new Date(ev.created_at).toLocaleString()}</span>
                    <span className="rounded bg-slate-100 px-1 dark:bg-slate-800">{ev.event_type}</span>
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
