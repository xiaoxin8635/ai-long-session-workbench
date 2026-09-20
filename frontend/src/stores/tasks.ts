/**
 * 任务 store（EchoDesk 前端，M-F5）。
 *
 * 职责：workspace 引导、任务列表（状态过滤）、创建、状态流转与
 * 优先级切换。所有变更成功后回刷当前过滤视图。
 */
import { create } from "zustand";
import * as tasksApi from "../api/tasks";
import type { Task, TaskStatus } from "../api/tasks";
import { listWorkspaces } from "../api/workspaces";
import { errorMessage } from "./auth";

/** 任务 store 状态与动作。 */
interface TasksState {
  /** 当前 workspace ID（bootstrap 后填充）。 */
  workspaceId: string | null;
  /** 当前过滤视图下的任务列表。 */
  tasks: Task[];
  /** 状态过滤（null = 全部）。 */
  filterStatus: TaskStatus | null;
  /** 创建中标记。 */
  creating: boolean;
  /** 面向用户的错误文案（null 无错误）。 */
  error: string | null;

  /** 引导：取第一个 workspace 并拉取任务列表。 */
  bootstrap: () => Promise<void>;
  /** 按当前过滤条件拉取任务列表。 */
  fetchTasks: () => Promise<void>;
  /**
   * 切换状态过滤并回到该视图。
   *
   * @param status - 目标状态（null 全部）。
   */
  setFilter: (status: TaskStatus | null) => Promise<void>;
  /**
   * 创建任务。
   *
   * @param title - 标题（空串忽略）。
   * @param priority - 0 普通 / 1 高。
   * @returns 是否创建成功。
   */
  create: (title: string, priority: number) => Promise<boolean>;
  /**
   * 更新任务状态（成功后回刷列表）。
   *
   * @param taskId - 任务 ID。
   * @param status - 目标状态。
   */
  updateStatus: (taskId: string, status: TaskStatus) => Promise<void>;
  /**
   * 切换任务优先级（0 ↔ 1）。
   *
   * @param taskId - 任务 ID。
   * @param priority - 目标优先级。
   */
  setPriority: (taskId: string, priority: number) => Promise<void>;
  /** 清除错误提示。 */
  clearError: () => void;
}

export const useTasksStore = create<TasksState>()((set, get) => ({
  workspaceId: null,
  tasks: [],
  filterStatus: null,
  creating: false,
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

  fetchTasks: async () => {
    const { workspaceId, filterStatus } = get();
    if (workspaceId === null) {
      return;
    }
    try {
      const tasks = await tasksApi.listTasks(workspaceId, filterStatus ?? undefined);
      set({ tasks });
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  setFilter: async (status) => {
    set({ filterStatus: status });
    await get().fetchTasks();
  },

  create: async (title, priority) => {
    const { workspaceId } = get();
    const trimmed = title.trim();
    if (workspaceId === null || trimmed === "") {
      return false;
    }
    set({ creating: true, error: null });
    try {
      await tasksApi.createTask(workspaceId, { title: trimmed, priority });
      await get().fetchTasks();
      return true;
    } catch (err) {
      set({ error: errorMessage(err) });
      return false;
    } finally {
      set({ creating: false });
    }
  },

  updateStatus: async (taskId, status) => {
    const { workspaceId } = get();
    if (workspaceId === null) {
      return;
    }
    try {
      await tasksApi.updateTask(workspaceId, taskId, { status });
      await get().fetchTasks();
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  setPriority: async (taskId, priority) => {
    const { workspaceId } = get();
    if (workspaceId === null) {
      return;
    }
    try {
      await tasksApi.updateTask(workspaceId, taskId, { priority });
      await get().fetchTasks();
    } catch (err) {
      set({ error: errorMessage(err) });
    }
  },

  clearError: () => set({ error: null }),
}));
