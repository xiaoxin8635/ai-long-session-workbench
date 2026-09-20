/**
 * 记忆工作台 store 单测（EchoDesk 前端）。
 *
 * mock api/memories 与 api/workspaces，验证：bootstrap、过滤分页、
 * 详情加载、编辑、软删除、冲突裁决、检索测试的状态流转。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/workspaces", () => ({
  listWorkspaces: vi.fn(),
}));
vi.mock("../api/memories", () => ({
  listMemories: vi.fn(),
  getMemory: vi.fn(),
  updateMemory: vi.fn(),
  deleteMemory: vi.fn(),
  resolveMemory: vi.fn(),
  searchMemories: vi.fn(),
}));

const { useMemoriesStore } = await import("../stores/memories");
const memoriesApi = await import("../api/memories");
const workspacesApi = await import("../api/workspaces");
import type { MemoryItem } from "../api/memories";

const listWorkspacesMock = vi.mocked(workspacesApi.listWorkspaces);
const listMemoriesMock = vi.mocked(memoriesApi.listMemories);
const getMemoryMock = vi.mocked(memoriesApi.getMemory);
const updateMemoryMock = vi.mocked(memoriesApi.updateMemory);
const deleteMemoryMock = vi.mocked(memoriesApi.deleteMemory);
const resolveMemoryMock = vi.mocked(memoriesApi.resolveMemory);
const searchMemoriesMock = vi.mocked(memoriesApi.searchMemories);

/**
 * 构造记忆条目。
 *
 * @param id - ID。
 * @param patch - 覆盖字段。
 * @returns MemoryItem 形状对象。
 */
function mkMemory(id: string, patch: Partial<MemoryItem> = {}): MemoryItem {
  return {
    id,
    memory_type: "semantic",
    key: `fact.test.${id}`,
    content: "测试内容",
    confidence: 0.9,
    importance: 0.8,
    status: "active",
    version: 1,
    source_session_id: null,
    source_message_ids: [],
    supersedes_id: null,
    expires_at: null,
    hit_count: 0,
    last_hit_at: null,
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
    ...patch,
  };
}

/** 重置 store 至初始态。 */
function resetStore(): void {
  useMemoriesStore.setState({
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
  });
}

describe("memories store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore();
    listWorkspacesMock.mockResolvedValue([
      { id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "" },
    ]);
    listMemoriesMock.mockResolvedValue({ items: [], total: 0 });
  });

  it("bootstrap 后按过滤条件拉取列表", async () => {
    await useMemoriesStore.getState().bootstrap();
    listMemoriesMock.mockResolvedValue({ items: [mkMemory("m-1")], total: 1 });

    await useMemoriesStore.getState().fetchPage(0);

    expect(useMemoriesStore.getState().workspaceId).toBe("ws-1");
    expect(useMemoriesStore.getState().items).toHaveLength(1);
    expect(listMemoriesMock).toHaveBeenCalledWith("ws-1", { limit: 20, offset: 0 });
  });

  it("setFilters 携带过滤条件回到第一页", async () => {
    useMemoriesStore.setState({ workspaceId: "ws-1" });
    useMemoriesStore.setState({ offset: 40 });

    useMemoriesStore.getState().setFilters({ type: "semantic", status: "conflicted", q: "团队" });
    await vi.waitFor(() => {
      expect(listMemoriesMock).toHaveBeenCalled();
    });

    const filters = listMemoriesMock.mock.calls[0]![1];
    expect(filters).toMatchObject({ memory_type: "semantic", status: "conflicted", q: "团队", offset: 0 });
    expect(useMemoriesStore.getState().offset).toBe(0);
  });

  it("loadDetail 拉取版本链与事件流水", async () => {
    useMemoriesStore.setState({ workspaceId: "ws-1" });
    getMemoryMock.mockResolvedValue({
      ...mkMemory("m-1"),
      version_chain: [mkMemory("m-0", { version: 0, status: "superseded" })],
      events: [{ event_type: "create", old_value: null, new_value: "测试内容", source: "llm", created_at: "" }],
    });

    await useMemoriesStore.getState().loadDetail("m-1");

    expect(useMemoriesStore.getState().detail?.version_chain).toHaveLength(1);
    expect(useMemoriesStore.getState().detail?.events).toHaveLength(1);
  });

  it("editMemory 提交部分更新并刷新详情与列表", async () => {
    useMemoriesStore.setState({ workspaceId: "ws-1", offset: 0 });
    updateMemoryMock.mockResolvedValue(mkMemory("m-1", { content: "新内容", version: 2 }));
    getMemoryMock.mockResolvedValue({
      ...mkMemory("m-1"),
      content: "新内容",
      version: 2,
      version_chain: [],
      events: [],
    });

    const ok = await useMemoriesStore.getState().editMemory("m-1", { content: "新内容" });

    expect(ok).toBe(true);
    expect(updateMemoryMock).toHaveBeenCalledWith("ws-1", "m-1", { content: "新内容" });
    expect(useMemoriesStore.getState().detail?.content).toBe("新内容");
  });

  it("removeMemory 软删除并刷新列表", async () => {
    useMemoriesStore.setState({ workspaceId: "ws-1", offset: 0 });
    deleteMemoryMock.mockResolvedValue(undefined);
    listMemoriesMock.mockResolvedValue({ items: [], total: 0 });

    await useMemoriesStore.getState().removeMemory("m-1");

    expect(deleteMemoryMock).toHaveBeenCalledWith("ws-1", "m-1");
    expect(useMemoriesStore.getState().items).toHaveLength(0);
  });

  it("resolveMemory 提交裁决方向", async () => {
    useMemoriesStore.setState({ workspaceId: "ws-1", offset: 0 });
    resolveMemoryMock.mockResolvedValue(mkMemory("m-1", { status: "active" }));

    await useMemoriesStore.getState().resolveMemory("m-1", "this");

    expect(resolveMemoryMock).toHaveBeenCalledWith("ws-1", "m-1", "this");
  });

  it("search 保存命中列表", async () => {
    useMemoriesStore.setState({ workspaceId: "ws-1" });
    searchMemoriesMock.mockResolvedValue([
      {
        id: "m-1",
        memory_type: "semantic",
        key: "fact.team",
        content: "团队代号猎鹰",
        confidence: 0.9,
        importance: 0.8,
        score: 0.87,
        similarity: 0.83,
      },
    ]);

    await useMemoriesStore.getState().search("团队代号");

    expect(useMemoriesStore.getState().searchHits).toHaveLength(1);
    expect(useMemoriesStore.getState().searchHits[0]!.score).toBeCloseTo(0.87);
  });
});
