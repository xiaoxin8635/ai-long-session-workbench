/**
 * 知识库 store（EchoDesk 前端，M-F5）。
 *
 * 职责：workspace 引导、文件列表拉取、上传（校验后调 API）、删除、
 * 检索调试。轮询（parsing → embedded）由页面层基于 hasParsing 驱动。
 */
import { create } from "zustand";
import * as knowledgeApi from "../api/knowledge";
import type { KnowledgeFile, KnowledgeHit } from "../api/knowledge";
import { listWorkspaces } from "../api/workspaces";
import { errorMessage } from "./auth";

/** 知识库 store 状态与动作。 */
interface KnowledgeState {
  /** 当前 workspace ID（bootstrap 后填充）。 */
  workspaceId: string | null;
  /** 文件列表（新上传在前）。 */
  files: KnowledgeFile[];
  /** 检索命中列表。 */
  searchHits: KnowledgeHit[];
  /** 最近一次检索的 query（回显对照）。 */
  searchQuery: string;
  /** 上传中标记。 */
  uploading: boolean;
  /** 面向用户的错误文案（null 无错误）。 */
  error: string | null;

  /** 引导：取第一个 workspace 并拉取文件列表。 */
  bootstrap: () => Promise<void>;
  /** 拉取文件列表。 */
  fetchFiles: () => Promise<void>;
  /**
   * 上传文件（客户端先校验扩展名与大小）。
   *
   * @param file - 浏览器 File 对象。
   * @returns 是否上传成功。
   */
  upload: (file: File) => Promise<boolean>;
  /**
   * 删除文件（级联切片）。
   *
   * @param fileId - 文件 ID。
   */
  remove: (fileId: string) => Promise<void>;
  /**
   * 检索调试。
   *
   * @param query - 检索查询（空串忽略）。
   */
  search: (query: string) => Promise<void>;
  /** 清除错误提示。 */
  clearError: () => void;
}

export const useKnowledgeStore = create<KnowledgeState>()((set, get) => ({
  workspaceId: null,
  files: [],
  searchHits: [],
  searchQuery: "",
  uploading: false,
  error: null,

  bootstrap: async () => {
    try {
      const workspaces = await listWorkspaces();
      const wsId = workspaces[0]?.id ?? null;
      set({ workspaceId: wsId });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  fetchFiles: async () => {
    const { workspaceId } = get();
    if (workspaceId === null) {
      return;
    }
    try {
      const files = await knowledgeApi.listKnowledgeFiles(workspaceId);
      set({ files });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  upload: async (file) => {
    const { workspaceId } = get();
    if (workspaceId === null) {
      return false;
    }
    const lower = file.name.toLowerCase();
    const extOk = knowledgeApi.ALLOWED_EXTENSIONS.some((ext) => lower.endsWith(ext));
    if (!extOk) {
      set({ error: `仅支持 ${knowledgeApi.ALLOWED_EXTENSIONS.join(" / ")} 格式` });
      return false;
    }
    if (file.size > knowledgeApi.MAX_UPLOAD_BYTES) {
      set({ error: "文件超过 20MB 上限" });
      return false;
    }
    set({ uploading: true, error: null });
    try {
      await knowledgeApi.uploadKnowledgeFile(workspaceId, file);
      await get().fetchFiles();
      return true;
    } catch (err) {
      set({ error: errorMessage(err) });
      return false;
    } finally {
      set({ uploading: false });
    }
  },

  remove: async (fileId) => {
    const { workspaceId } = get();
    if (workspaceId === null) {
      return;
    }
    try {
      await knowledgeApi.deleteKnowledgeFile(workspaceId, fileId);
      await get().fetchFiles();
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  search: async (query) => {
    const { workspaceId } = get();
    if (workspaceId === null || query.trim() === "") {
      return;
    }
    try {
      const result = await knowledgeApi.searchKnowledge(workspaceId, query.trim());
      set({ searchHits: result.hits, searchQuery: result.query, error: null });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  clearError: () => set({ error: null }),
}));
