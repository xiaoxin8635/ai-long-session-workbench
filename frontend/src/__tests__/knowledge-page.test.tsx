/**
 * 知识库页冒烟测试（EchoDesk 前端）。
 *
 * mock api 层，验证上传区、文件状态徽标列表与检索调试区渲染。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("../api/workspaces", () => ({
  listWorkspaces: vi.fn().mockResolvedValue([
    { id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "" },
  ]),
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

const { default: KnowledgePage } = await import("../pages/KnowledgePage");
const knowledgeApi = await import("../api/knowledge");
import type { KnowledgeFile } from "../api/knowledge";

const listFilesMock = vi.mocked(knowledgeApi.listKnowledgeFiles);

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

describe("KnowledgePage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    listFilesMock.mockResolvedValue([
      mkFile("f-1"),
      mkFile("f-2", { status: "parsing", chunk_count: 0 }),
    ]);
  });

  it("渲染上传区、文件卡片与状态徽标", async () => {
    render(<KnowledgePage />);
    expect(await screen.findByText("上传文档")).toBeTruthy();
    expect(await screen.findByText("doc-f-1.md")).toBeTruthy();
    expect(screen.getAllByText("已就绪").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("解析中").length).toBeGreaterThanOrEqual(1);
  });

  it("渲染检索调试区", async () => {
    render(<KnowledgePage />);
    expect(await screen.findByText("检索调试（与对话 RAG 同口径）")).toBeTruthy();
  });
});
