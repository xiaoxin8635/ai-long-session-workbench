/**
 * 模块占位页（EchoDesk 前端，M-F1 骨架用）。
 *
 * M-F5 逐个替换为完整实现：知识库/任务/用量/设置。
 */

/**
 * 占位页通用组件。
 *
 * @param props - title 页面标题；hint 交付里程碑提示。
 * @returns 居中占位卡片。
 */
export function Placeholder({ title, hint }: { title: string; hint: string }): JSX.Element {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="text-center">
        <h2 className="text-xl font-semibold">{title}</h2>
        <p className="mt-2 text-sm text-slate-500 dark:text-slate-400">{hint}</p>
      </div>
    </div>
  );
}

/** 知识库占位（M-F5 实现：上传/状态/检索调试）。 */
export function KnowledgePage(): JSX.Element {
  return <Placeholder title="知识库" hint="M-F5 交付：文档上传 + 检索调试" />;
}

/** 任务面板占位（M-F5 实现：列表/状态流转）。 */
export function TasksPage(): JSX.Element {
  return <Placeholder title="任务" hint="M-F5 交付：任务看板 + 状态流转" />;
}

/** 用量页占位（M-F5 实现：usage summary 图表）。 */
export function UsagePage(): JSX.Element {
  return <Placeholder title="用量" hint="M-F5 交付：token/成本统计图表" />;
}

/** 设置页占位（M-F5 实现：个人资料 + workspace 成员管理）。 */
export function SettingsPage(): JSX.Element {
  return <Placeholder title="设置" hint="M-F5 交付：个人资料 + 成员管理" />;
}
