/**
 * 知识库页（EchoDesk 前端 · 「宣纸书卷」古风，M-F5）。
 *
 * 文档上传（pdf/docx/md/txt ≤20MB）+ 状态机列表（parsing 自动轮询到
 * embedded/failed）+ 撤销式删除 + 检索调试（与 chat RAG 区块同口径）。
 */
import { useEffect, useRef, useState, type JSX } from "react";
import { Search, Upload, X } from "lucide-react";
import type { KnowledgeFile, KnowledgeFileStatus } from "../api/knowledge";
import { useKnowledgeStore } from "../stores/knowledge";
import { showToast } from "../stores/toast";

/** 状态徽标文案与样式（古风 token 口径）。 */
const STATUS_LABELS: Record<KnowledgeFileStatus, string> = {
  parsing: "解析中",
  embedded: "已就绪",
  failed: "失败",
  superseded: "旧版本",
};
const STATUS_STYLES: Record<KnowledgeFileStatus, string> = {
  parsing: "bg-warning/14 text-warning ring-1 ring-inset ring-warning/30",
  embedded: "bg-success/14 text-success ring-1 ring-inset ring-success/30",
  failed: "bg-danger/12 text-danger ring-1 ring-inset ring-danger/30",
  superseded: "bg-line/6 text-secondary ring-1 ring-inset ring-line/12",
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
  /** 延迟删除中的文件 ID（乐观隐藏 + 撤销窗口；null = 无）。 */
  const [pendingDeleteId, setPendingDeleteId] = useState<string | null>(null);
  /** 延迟删除计时器（撤销时清除）。 */
  const deleteTimerRef = useRef<number | null>(null);

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

  // 卸载时清理延迟删除计时器
  useEffect(() => {
    return () => {
      if (deleteTimerRef.current !== null) {
        window.clearTimeout(deleteTimerRef.current);
      }
    };
  }, []);

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

  /**
   * 删除文件（乐观隐藏 + 5s 撤销窗口；窗口过后才真正调用删除 API）。
   *
   * @param file - 待删除文件。
   */
  function handleDelete(file: KnowledgeFile): void {
    setPendingDeleteId(file.id);
    deleteTimerRef.current = window.setTimeout(() => {
      deleteTimerRef.current = null;
      setPendingDeleteId(null);
      void remove(file.id);
    }, 5000);
    showToast({
      message: `已删除「${file.filename}」`,
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

  return (
    <div className="anim-rise-in relative mx-auto max-w-4xl space-y-5 p-6">
      <div className="flex items-center justify-between">
        <h1 className="flex items-center gap-2 font-display text-lg font-semibold text-primary">
          <span className="bookmark-bar h-4" aria-hidden />
          知识库
        </h1>
        <span className="font-mono text-xs text-muted">共 {files.length} 个文件</span>
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

      {/* 上传区 */}
      <div className="rounded-xl border border-dashed border-line/25 bg-surface/40 p-6 text-center">
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
          className="btn-primary inline-flex items-center gap-2 rounded-xl px-4 py-2 text-sm"
        >
          <Upload className="size-4" strokeWidth={2.2} aria-hidden />
          {uploading ? "上传解析中…" : "上传文档"}
        </button>
        <p className="mt-2.5 text-xs text-muted">
          支持 pdf / docx / md / txt，≤20MB；上传后同步解析切片，向量化后台完成
        </p>
      </div>

      {/* 文件列表 */}
      <div className="space-y-2">
        {files
          .filter((f) => f.id !== pendingDeleteId)
          .map((f, i) => (
            <FileCard
              key={f.id}
              file={f}
              index={i}
              onDelete={() => handleDelete(f)}
            />
          ))}
        {files.length === 0 && (
          <p className="py-10 text-center font-display text-sm text-muted">
            书架尚空 · 上传后对话中的 RAG 区块将自动引用
          </p>
        )}
      </div>

      {/* 检索调试 */}
      <section className="rounded-xl border border-line/12 bg-surface/50 p-4 shadow-panel">
        <h2 className="flex items-center gap-2 font-display text-sm font-semibold text-primary">
          <span className="bookmark-bar h-3.5" aria-hidden />
          检索调试（与对话 RAG 同口径）
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
            placeholder="输入模拟 query，验证哪些切片会被召回"
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
          <p className="mt-3 text-xs text-muted">query「{searchQuery}」命中 {searchHits.length} 条：</p>
        )}
        <ul className="mt-1.5 space-y-1.5">
          {searchHits.map((h) => (
            <li key={h.chunk_id} className="rounded-lg border border-line/10 bg-elevated/70 px-3 py-2 text-xs">
              <div className="flex items-center gap-2">
                <span className="truncate font-medium text-primary">{h.filename}</span>
                <span className="shrink-0 font-mono text-muted">#chunk {h.chunk_index}</span>
                <span className="ml-auto shrink-0 font-mono text-muted">
                  score {h.score.toFixed(3)} · vec {h.vector_similarity.toFixed(3)}
                </span>
              </div>
              <p className="mt-1 line-clamp-3 text-secondary">{h.content}</p>
            </li>
          ))}
        </ul>
        {searchHits.length === 0 && (
          <p className="mt-2 text-xs text-muted">尚无检索结果</p>
        )}
      </section>
    </div>
  );
}

/**
 * 知识文件卡片。
 *
 * @param props - file 文件元信息；index 序号（入场 stagger）；onDelete 删除回调。
 * @returns 卡片 JSX。
 */
function FileCard({
  file,
  index,
  onDelete,
}: {
  file: KnowledgeFile;
  index: number;
  onDelete: () => void;
}): JSX.Element {
  return (
    <div
      className="card-paper anim-rise-in flex items-center gap-3 rounded-xl p-3"
      style={{ animationDelay: `${Math.min(index, 12) * 30}ms` }}
    >
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className={"rounded-full px-2 py-0.5 text-[10px] font-medium " + STATUS_STYLES[file.status]}>
            {STATUS_LABELS[file.status]}
          </span>
          <span className="rounded-full bg-line/6 px-2 py-0.5 font-mono text-[10px] uppercase text-secondary ring-1 ring-inset ring-line/12">
            {file.file_type}
          </span>
          <span className="truncate text-sm font-medium text-primary">{file.filename}</span>
        </div>
        <p className="mt-1 font-mono text-[10px] text-muted">
          v{file.version} · {file.chunk_count} 切片 · {new Date(file.created_at).toLocaleString()}
          {file.status === "failed" && "（可重传同名文件重试向量化）"}
        </p>
      </div>
      <button
        type="button"
        onClick={onDelete}
        aria-label={`删除 ${file.filename}`}
        className="shrink-0 rounded-lg px-2.5 py-1 text-xs text-danger transition-colors hover:bg-danger/12"
      >
        删除
      </button>
    </div>
  );
}
