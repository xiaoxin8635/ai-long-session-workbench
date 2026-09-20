/**
 * 记忆工作台状态 store（EchoDesk 前端，M-F4）。
 *
 * 职责：列表过滤分页、当前详情（版本链/事件流水）、编辑/软删除/冲突裁决、
 * 检索测试（与对话链路同检索器的命中预览）。
 */
import { create } from "zustand";
import * as memoriesApi from "../api/memories";
import type {
  MemoryDetail,
  MemoryFilters,
  MemoryItem,
  MemorySearchHit,
  MemoryStatus,
  MemoryType,
} from "../api/memories";
import { listWorkspaces } from "../api/workspaces";
import { errorMessage } from "./auth";

/** 页大小（与列表默认一致）。 */
const PAGE_SIZE = 20;

/** 记忆工作台 store 状态与动作。 */
interface MemoriesState {
  /** 当前 workspace（bootstrap 后填充）。 */
  workspaceId: string | null;
  /** 列表条目（当前页）。 */
  items: MemoryItem[];
  /** 服务端总数（分页用）。 */
  total: number;
  /** 当前页偏移。 */
  offset: number;
  /** 类型过滤（null = 全部）。 */
  filterType: MemoryType | null;
  /** 状态过滤（null = 全部）。 */
  filterStatus: MemoryStatus | null;
  /** 关键词过滤。 */
  filterQuery: string;
  /** 当前查看的详情（含版本链与事件）。 */
  detail: MemoryDetail | null;
  /** 检索测试命中。 */
  searchHits: MemorySearchHit[];
  /** 页面级错误提示。 */
  error: string | null;

  /** 初始化 workspace（登录后/刷新后调用）。 */
  bootstrap: () => Promise<void>;
  /** 按当前过滤条件拉取指定页。 */
  fetchPage: (offset?: number) => Promise<void>;
  /** 设置过滤条件并回到第一页。 */
  setFilters: (patch: { type?: MemoryType | null; status?: MemoryStatus | null; q?: string }) => void;
  /** 拉取详情（版本链 + 事件流水）。 */
  loadDetail: (memoryId: string) => Promise<void>;
  /** 关闭详情。 */
  closeDetail: () => void;
  /** 用户编辑（内容/置信度/重要度）。 */
  editMemory: (memoryId: string, patch: { content?: string; confidence?: number; importance?: number }) => Promise<boolean>;
  /** 软删除并刷新列表。 */
  removeMemory: (memoryId: string) => Promise<void>;
  /** 冲突裁决并刷新列表与详情。 */
  resolveMemory: (memoryId: string, keep: "this" | "other") => Promise<void>;
  /** 检索测试。 */
  search: (query: string, topK?: number) => Promise<void>;
  /** 清除错误提示。 */
  clearError: () => void;
}

export const useMemoriesStore = create<MemoriesState>()((set, get) => ({
  workspaceId: null,
  items: [],
  total: 0,
  offset: 0,
  filterType: null,
  filterStatus: null,
  filterQuery: "",
  detail: null,
  searchHits: [],
  error: null,

  bootstrap: async () => {
    try {
      const wsId = (await listWorkspaces())[0]?.id ?? null;
      set({ workspaceId: wsId });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  fetchPage: async (offset = 0) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return;
    }
    const { filterType, filterStatus, filterQuery } = get();
    try {
      const filters: MemoryFilters = { limit: PAGE_SIZE, offset };
      if (filterType !== null) {
        filters.memory_type = filterType;
      }
      if (filterStatus !== null) {
        filters.status = filterStatus;
      }
      if (filterQuery.trim() !== "") {
        filters.q = filterQuery.trim();
      }
      const page = await memoriesApi.listMemories(wsId, filters);
      set({ items: page.items, total: page.total, offset });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  setFilters: (patch) => {
    set({
      filterType: patch.type ?? get().filterType,
      filterStatus: patch.status ?? get().filterStatus,
      filterQuery: patch.q ?? get().filterQuery,
    });
    void get().fetchPage(0);
  },

  loadDetail: async (memoryId) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return;
    }
    try {
      const detail = await memoriesApi.getMemory(wsId, memoryId);
      set({ detail });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  closeDetail: () => {
    set({ detail: null });
  },

  editMemory: async (memoryId, patch) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return false;
    }
    try {
      await memoriesApi.updateMemory(wsId, memoryId, patch);
      await get().loadDetail(memoryId);
      await get().fetchPage(get().offset);
      return true;
    } catch (err) {
      set({ error: errorMessage(err) });
      return false;
    }
  },

  removeMemory: async (memoryId) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return;
    }
    try {
      await memoriesApi.deleteMemory(wsId, memoryId);
      get().closeDetail();
      await get().fetchPage(get().offset);
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  resolveMemory: async (memoryId, keep) => {
    const wsId = get().workspaceId;
    if (!wsId) {
      return;
    }
    try {
      await memoriesApi.resolveMemory(wsId, memoryId, keep);
      get().closeDetail();
      await get().fetchPage(get().offset);
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  search: async (query, topK = 10) => {
    const wsId = get().workspaceId;
    if (!wsId || query.trim() === "") {
      return;
    }
    try {
      const hits = await memoriesApi.searchMemories(wsId, query.trim(), topK);
      set({ searchHits: hits });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  clearError: () => {
    set({ error: null });
  },
}));
