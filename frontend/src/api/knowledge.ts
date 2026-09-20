/**
 * 知识库 API（EchoDesk 前端，M-F5）。
 *
 * 对接 memory-service `/api/knowledge/*`：上传（multipart，同步解析切片 +
 * 后台向量化）、文件列表（状态机 + 版本 + 切片数）、删除（级联切片）、
 * 检索调试（向量 + BM25 → RRF → rerank，与 chat RAG 区块同口径）。
 * workspace_id 一律走 query 参数（CurrentWorkspaceQuery）。
 */
import { api } from "./client";

/** 知识文件摄取状态机（服务端 KnowledgeFileStatus）。 */
export type KnowledgeFileStatus = "parsing" | "embedded" | "failed" | "superseded";

/** 知识文件元信息。 */
export interface KnowledgeFile {
  id: string;
  filename: string;
  file_type: string;
  status: KnowledgeFileStatus;
  version: number;
  chunk_count: number;
  created_at: string;
}

/** 检索调试命中项（含正文与向量相似度）。 */
export interface KnowledgeHit {
  chunk_id: string;
  file_id: string;
  filename: string;
  chunk_index: number;
  content: string;
  score: number;
  vector_similarity: number;
}

/** 检索调试响应（回显 query 便于对照）。 */
export interface KnowledgeSearchResult {
  query: string;
  hits: KnowledgeHit[];
}

/** 上传大小上限（与服务端一致：20MB）。 */
export const MAX_UPLOAD_BYTES = 20 * 1024 * 1024;

/** 允许的文件扩展名（与服务端解析器支持范围一致）。 */
export const ALLOWED_EXTENSIONS = [".pdf", ".docx", ".md", ".txt"];

/**
 * 上传知识文件（multipart）。
 *
 * @param workspaceId - workspace ID（query 参数）。
 * @param file - 浏览器 File 对象（pdf/docx/md/txt，≤20MB）。
 * @returns 新建文件元信息（status=parsing，embedding 后台补齐）。
 */
export async function uploadKnowledgeFile(
  workspaceId: string,
  file: File
): Promise<KnowledgeFile> {
  const form = new FormData();
  form.append("file", file);
  const { data } = await api.post<KnowledgeFile>("/api/knowledge/files", form, {
    params: { workspace_id: workspaceId },
  });
  return data;
}

/**
 * 知识文件列表（含 superseded 历史版本，新上传在前）。
 *
 * @param workspaceId - workspace ID。
 * @returns 文件列表。
 */
export async function listKnowledgeFiles(workspaceId: string): Promise<KnowledgeFile[]> {
  const { data } = await api.get<KnowledgeFile[]>("/api/knowledge/files", {
    params: { workspace_id: workspaceId },
  });
  return data;
}

/**
 * 删除知识文件（硬删，级联切片）。
 *
 * @param workspaceId - workspace ID。
 * @param fileId - 文件 ID。
 */
export async function deleteKnowledgeFile(workspaceId: string, fileId: string): Promise<void> {
  await api.delete(`/api/knowledge/files/${fileId}`, {
    params: { workspace_id: workspaceId },
  });
}

/**
 * 知识检索调试（与 chat RAG 区块同口径）。
 *
 * @param workspaceId - workspace ID。
 * @param query - 检索查询。
 * @param topK - 返回条数（1-20，缺省用服务端配置）。
 * @returns 检索结果。
 */
export async function searchKnowledge(
  workspaceId: string,
  query: string,
  topK?: number
): Promise<KnowledgeSearchResult> {
  const { data } = await api.post<KnowledgeSearchResult>(
    "/api/knowledge/search",
    { query, ...(topK !== undefined ? { top_k: topK } : {}) },
    { params: { workspace_id: workspaceId } }
  );
  return data;
}
