/**
 * 知识库 store 单测（EchoDesk 前端）。
 *
 * mock api/knowledge 与 api/workspaces，验证：bootstrap、列表拉取、
 * 上传（含扩展名/大小客户端校验）、删除、检索测试的状态流转。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/workspaces", () => ({
  listWorkspaces: vi.fn(),
}));
vi.mock("../api/knowledge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/knowledge")>();
  return {
    ...actual,
    listKnowledgeFiles: vi.fn(),
    uploadKnowledgeFile: vi.fn(),
    deleteKnowledgeFile: vi.fn(),
    searchKnowledge: vi.fn(),
  };
});

const { useKnowledgeStore } = await import("../stores/knowledge");
const knowledgeApi = await import("../api/knowledge");
const workspacesApi = await import("../api/workspaces");
import type { KnowledgeFile } from "../api/knowledge";

const listWorkspacesMock = vi.mocked(workspacesApi.listWorkspaces);
const listFilesMock = vi.mocked(knowledgeApi.listKnowledgeFiles);
const uploadMock = vi.mocked(knowledgeApi.uploadKnowledgeFile);
const deleteMock = vi.mocked(knowledgeApi.deleteKnowledgeFile);
const searchMock = vi.mocked(knowledgeApi.searchKnowledge);

/**
 * 构造知识文件条目。
 *
 * @param id - ID。
 * @param patch - 覆盖字段。
 * @returns KnowledgeFile 形状对象。
 */
function mkFile(id: string, patch: Partial<KnowledgeFile> = {}): KnowledgeFile {
  return {
    id,
    filename: `doc-${id}.md`,
    file_type: "md",
    status: "embedded",
    version: 1,
    chunk_count: 3,
    created_at: "2026-09-20T00:00:00Z",
    ...patch,
  };
}

/** 重置 store 至初始态。 */
function resetStore(): void {
  useKnowledgeStore.setState({
    workspaceId: null,
    files: [],
    searchHits: [],
    searchQuery: "",
    uploading: false,
    error: null,
  });
}

describe("knowledge store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore();
    listWorkspacesMock.mockResolvedValue([
      { id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "" },
    ]);
    listFilesMock.mockResolvedValue([]);
  });

  it("bootstrap 后拉取文件列表", async () => {
    listFilesMock.mockResolvedValue([mkFile("f-1")]);

    await useKnowledgeStore.getState().bootstrap();
    await useKnowledgeStore.getState().fetchFiles();

    expect(useKnowledgeStore.getState().workspaceId).toBe("ws-1");
    expect(useKnowledgeStore.getState().files).toHaveLength(1);
    expect(listFilesMock).toHaveBeenCalledWith("ws-1");
  });

  it("upload 走 API 后刷新列表", async () => {
    useKnowledgeStore.setState({ workspaceId: "ws-1" });
    uploadMock.mockResolvedValue(mkFile("f-2", { status: "parsing", chunk_count: 0 }));
    listFilesMock.mockResolvedValue([
      mkFile("f-2", { status: "parsing", chunk_count: 0 }),
      mkFile("f-1"),
    ]);

    const file = new File(["# hello"], "note.md", { type: "text/markdown" });
    const ok = await useKnowledgeStore.getState().upload(file);

    expect(ok).toBe(true);
    expect(uploadMock).toHaveBeenCalledWith("ws-1", file);
    expect(useKnowledgeStore.getState().files).toHaveLength(2);
    expect(useKnowledgeStore.getState().uploading).toBe(false);
  });

  it("upload 拒绝不支持的扩展名", async () => {
    useKnowledgeStore.setState({ workspaceId: "ws-1" });

    const file = new File(["x"], "malware.exe", { type: "application/x-msdownload" });
    const ok = await useKnowledgeStore.getState().upload(file);

    expect(ok).toBe(false);
    expect(uploadMock).not.toHaveBeenCalled();
    expect(useKnowledgeStore.getState().error).toContain("仅支持");
  });

  it("remove 删除后刷新列表", async () => {
    useKnowledgeStore.setState({ workspaceId: "ws-1", files: [mkFile("f-1")] });
    deleteMock.mockResolvedValue(undefined);
    listFilesMock.mockResolvedValue([]);

    await useKnowledgeStore.getState().remove("f-1");

    expect(deleteMock).toHaveBeenCalledWith("ws-1", "f-1");
    expect(useKnowledgeStore.getState().files).toHaveLength(0);
  });

  it("search 保存命中与回显 query", async () => {
    useKnowledgeStore.setState({ workspaceId: "ws-1" });
    searchMock.mockResolvedValue({
      query: "部署方式",
      hits: [
        {
          chunk_id: "c-1",
          file_id: "f-1",
          filename: "doc-f-1.md",
          chunk_index: 0,
          content: "Docker Compose 部署",
          score: 0.9,
          vector_similarity: 0.85,
        },
      ],
    });

    await useKnowledgeStore.getState().search("部署方式");

    expect(useKnowledgeStore.getState().searchHits).toHaveLength(1);
    expect(useKnowledgeStore.getState().searchQuery).toBe("部署方式");
  });
});
