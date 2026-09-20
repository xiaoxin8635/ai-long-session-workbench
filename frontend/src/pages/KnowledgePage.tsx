/**
 * 知识库页（EchoDesk 前端，M-F5）。
 *
 * 文档上传（pdf/docx/md/txt ≤20MB）+ 状态机列表（parsing 自动轮询到
 * embedded/failed）+ 删除 + 检索调试（与 chat RAG 区块同口径）。
 */
import { useEffect, useRef, useState, type JSX } from "react";
import type { KnowledgeFile, KnowledgeFileStatus } from "../api/knowledge";
import { useKnowledgeStore } from "../stores/knowledge";

/** 状态徽标文案与样式。 */
const STATUS_LABELS: Record<KnowledgeFileStatus, string> = {
  parsing: "解析中",
  embedded: "已就绪",
  failed: "失败",
  superseded: "旧版本",
};
const STATUS_STYLES: Record<KnowledgeFileStatus, string> = {
  parsing: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-400",
  embedded: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-400",
  failed: "bg-red-100 text-red-600 dark:bg-red-950 dark:text-red-400",
  superseded: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
};

/**
 * 知识库页组件。
 *
 * @returns 页面 JSX。
 */
export default function KnowledgePage(): JSX.Element {
  const files = useKnowledgeStore((s) => s.files);
  const searchHits = useKnowledgeStore((s) => s.searchHits);
  const searchQuery = useKnowledgeStore((s) => s.searchQuery);
  const uploading = useKnowledgeStore((s) => s.uploading);
  const error = useKnowledgeStore((s) => s.error);
  const bootstrap = useKnowledgeStore((s) => s.bootstrap);
  const upload = useKnowledgeStore((s) => s.upload);
  const remove = useKnowledgeStore((s) => s.remove);
  const search = useKnowledgeStore((s) => s.search);
  const clearError = useKnowledgeStore((s) => s.clearError);

  /** 隐藏 file input 的 ref（按钮代理触发）。 */
  const fileInputRef = useRef<HTMLInputElement>(null);
  /** 检索调试输入草稿。 */
  const [searchDraft, setSearchDraft] = useState("");

  // 首次进入：取 workspace + 文件列表
  useEffect(() => {
    void bootstrap().then(() => {
      void useKnowledgeStore.getState().fetchFiles();
    });
  }, [bootstrap]);

  // 存在 parsing 文件时每 3s 轮询状态机（embedding 后台补齐）
  const hasParsing = files.some((f) => f.status === "parsing");
  useEffect(() => {
    if (!hasParsing) {
      return;
    }
    const timer = window.setInterval(() => {
      void useKnowledgeStore.getState().fetchFiles();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [hasParsing]);

  /**
   * 文件选择后立即上传（清空 input value 允许重复选同一文件）。
   *
   * @param fileList - 用户选择的文件列表。
   */
  function onFilePicked(fileList: FileList | null): void {
    const file = fileList?.[0];
    if (file !== undefined) {
      void upload(file);
    }
    if (fileInputRef.current !== null) {
      fileInputRef.current.value = "";
    }
  }

  return (
    <div className="relative mx-auto max-w-4xl space-y-5 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-semibold">知识库</h1>
        <span className="text-xs text-slate-400">共 {files.length} 个文件</span>
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

      {/* 上传区 */}
      <div className="rounded-xl border border-dashed border-slate-300 p-5 text-center dark:border-slate-700">
        <input
          ref={fileInputRef}
          type="file"
          accept=".pdf,.docx,.md,.txt"
          className="hidden"
          onChange={(e) => onFilePicked(e.target.files)}
        />
        <button
          type="button"
          disabled={uploading}
          onClick={() => fileInputRef.current?.click()}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-sky-700 disabled:opacity-50"
        >
          {uploading ? "上传解析中…" : "上传文档"}
        </button>
        <p className="mt-2 text-xs text-slate-400">
          支持 pdf / docx / md / txt，≤20MB；上传后同步解析切片，向量化后台完成
        </p>
      </div>

      {/* 文件列表 */}
      <div className="space-y-2">
        {files.map((f) => (
          <FileCard key={f.id} file={f} onDelete={() => {
            if (window.confirm(`删除「${f.filename}」？其全部切片将从检索中移除。`)) {
              void remove(f.id);
            }
          }} />
        ))}
        {files.length === 0 && (
          <p className="py-8 text-center text-xs text-slate-400">
            暂无文档（上传后对话中的 RAG 区块将自动引用）
          </p>
        )}
      </div>

      {/* 检索调试 */}
      <section className="rounded-xl border border-slate-200 p-4 dark:border-slate-800">
        <h2 className="text-sm font-semibold">检索调试（与对话 RAG 同口径）</h2>
        <div className="mt-2 flex gap-2">
          <input
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                void search(searchDraft);
              }
            }}
            placeholder="输入模拟 query，验证哪些切片会被召回"
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
          <p className="mt-3 text-xs text-slate-400">query「{searchQuery}」命中 {searchHits.length} 条：</p>
        )}
        <ul className="mt-1.5 space-y-1.5">
          {searchHits.map((h) => (
            <li key={h.chunk_id} className="rounded-lg bg-slate-50 px-3 py-2 text-xs dark:bg-slate-900">
              <div className="flex items-center gap-2">
                <span className="truncate font-medium">{h.filename}</span>
                <span className="shrink-0 text-slate-400">#chunk {h.chunk_index}</span>
                <span className="ml-auto shrink-0 text-slate-400">
                  score {h.score.toFixed(3)} · vec {h.vector_similarity.toFixed(3)}
                </span>
              </div>
              <p className="mt-1 line-clamp-3 text-slate-600 dark:text-slate-300">{h.content}</p>
            </li>
          ))}
        </ul>
        {searchHits.length === 0 && (
          <p className="mt-2 text-xs text-slate-400">尚无检索结果</p>
        )}
      </section>
    </div>
  );
}

/**
 * 知识文件卡片。
 *
 * @param props - file 文件元信息；onDelete 删除回调。
 * @returns 卡片 JSX。
 */
function FileCard({ file, onDelete }: { file: KnowledgeFile; onDelete: () => void }): JSX.Element {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-slate-200 bg-white p-3 dark:border-slate-800 dark:bg-slate-900">
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className={"rounded px-1.5 py-0.5 text-[10px] " + STATUS_STYLES[file.status]}>
            {STATUS_LABELS[file.status]}
          </span>
          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] uppercase text-slate-500 dark:bg-slate-800 dark:text-slate-400">
            {file.file_type}
          </span>
          <span className="truncate text-sm font-medium">{file.filename}</span>
        </div>
        <p className="mt-1 text-[10px] text-slate-400">
          v{file.version} · {file.chunk_count} 切片 · {new Date(file.created_at).toLocaleString()}
          {file.status === "failed" && "（可重传同名文件重试向量化）"}
        </p>
      </div>
      <button
        type="button"
        onClick={onDelete}
        className="shrink-0 text-xs text-red-500 transition hover:text-red-600"
      >
        删除
      </button>
    </div>
  );
}
