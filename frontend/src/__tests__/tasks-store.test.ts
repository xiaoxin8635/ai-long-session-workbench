/**
 * 任务 store 单测（EchoDesk 前端）。
 *
 * mock api/tasks 与 api/workspaces，验证：bootstrap、状态过滤、创建、
 * 状态流转与优先级切换的状态流转。
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/workspaces", () => ({
  listWorkspaces: vi.fn(),
}));
vi.mock("../api/tasks", () => ({
  listTasks: vi.fn(),
  createTask: vi.fn(),
  updateTask: vi.fn(),
}));

const { useTasksStore } = await import("../stores/tasks");
const tasksApi = await import("../api/tasks");
const workspacesApi = await import("../api/workspaces");
import type { Task } from "../api/tasks";

const listWorkspacesMock = vi.mocked(workspacesApi.listWorkspaces);
const listTasksMock = vi.mocked(tasksApi.listTasks);
const createTaskMock = vi.mocked(tasksApi.createTask);
const updateTaskMock = vi.mocked(tasksApi.updateTask);

/**
 * 构造任务条目。
 *
 * @param id - ID。
 * @param patch - 覆盖字段。
 * @returns Task 形状对象。
 */
function mkTask(id: string, patch: Partial<Task> = {}): Task {
  return {
    id,
    title: `任务 ${id}`,
    status: "open",
    priority: 0,
    due_date: null,
    related_session_ids: [],
    created_at: "2026-09-20T00:00:00Z",
    updated_at: "2026-09-20T00:00:00Z",
    ...patch,
  };
}

/** 重置 store 至初始态。 */
function resetStore(): void {
  useTasksStore.setState({
    workspaceId: null,
    tasks: [],
    filterStatus: null,
    creating: false,
    error: null,
  });
}

describe("tasks store", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore();
    listWorkspacesMock.mockResolvedValue([
      { id: "ws-1", name: "我的空间", owner_id: "u-1", created_at: "" },
    ]);
    listTasksMock.mockResolvedValue([]);
  });

  it("bootstrap 后按当前过滤拉取列表", async () => {
    listTasksMock.mockResolvedValue([mkTask("t-1")]);

    await useTasksStore.getState().bootstrap();
    await useTasksStore.getState().fetchTasks();

    expect(useTasksStore.getState().workspaceId).toBe("ws-1");
    expect(useTasksStore.getState().tasks).toHaveLength(1);
    expect(listTasksMock).toHaveBeenCalledWith("ws-1", undefined);
  });

  it("setFilter 携带状态参数重新拉取", async () => {
    useTasksStore.setState({ workspaceId: "ws-1" });

    await useTasksStore.getState().setFilter("doing");

    expect(useTasksStore.getState().filterStatus).toBe("doing");
    expect(listTasksMock).toHaveBeenLastCalledWith("ws-1", "doing");
  });

  it("create 提交标题与优先级并刷新", async () => {
    useTasksStore.setState({ workspaceId: "ws-1" });
    createTaskMock.mockResolvedValue(mkTask("t-2", { priority: 1 }));

    const ok = await useTasksStore.getState().create("写周报", 1);

    expect(ok).toBe(true);
    expect(createTaskMock).toHaveBeenCalledWith("ws-1", { title: "写周报", priority: 1 });
    expect(useTasksStore.getState().creating).toBe(false);
  });

  it("create 空标题直接拒绝", async () => {
    useTasksStore.setState({ workspaceId: "ws-1" });

    const ok = await useTasksStore.getState().create("   ", 0);

    expect(ok).toBe(false);
    expect(createTaskMock).not.toHaveBeenCalled();
  });

  it("updateStatus PATCH 后刷新列表", async () => {
    useTasksStore.setState({ workspaceId: "ws-1" });
    updateTaskMock.mockResolvedValue(mkTask("t-1", { status: "doing" }));

    await useTasksStore.getState().updateStatus("t-1", "doing");

    expect(updateTaskMock).toHaveBeenCalledWith("ws-1", "t-1", { status: "doing" });
    expect(listTasksMock).toHaveBeenCalled();
  });

  it("setPriority PATCH 后刷新列表", async () => {
    useTasksStore.setState({ workspaceId: "ws-1" });
    updateTaskMock.mockResolvedValue(mkTask("t-1", { priority: 1 }));

    await useTasksStore.getState().setPriority("t-1", 1);

    expect(updateTaskMock).toHaveBeenCalledWith("ws-1", "t-1", { priority: 1 });
  });
});
