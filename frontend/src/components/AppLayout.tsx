/**
 * 应用主布局（EchoDesk 前端）。
 *
 * 左侧固定侧边栏（品牌 + 模块导航 + 用户区/登出）+ 右侧 <Outlet /> 主区域。
 * M-F1 阶段各模块页为占位，后续里程碑逐个充实。
 */
import { NavLink, Outlet, useNavigate } from "react-router-dom";
import { useAuthStore } from "../stores/auth";

/** 侧边栏导航项定义。 */
interface NavItem {
  /** 路由路径。 */
  to: string;
  /** 展示名。 */
  label: string;
  /** emoji 图标（M-F1 占位，后续可换图标库）。 */
  icon: string;
}

/** 主导航清单（顺序即侧边栏展示顺序）。 */
const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "对话", icon: "💬" },
  { to: "/memories", label: "记忆", icon: "🧠" },
  { to: "/knowledge", label: "知识库", icon: "📚" },
  { to: "/tasks", label: "任务", icon: "✅" },
  { to: "/usage", label: "用量", icon: "📊" },
  { to: "/settings", label: "设置", icon: "⚙️" },
];

/** 页面组件：主布局壳。 */
export default function AppLayout(): JSX.Element {
  const navigate = useNavigate();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  /**
   * 登出并回到登录页。
   */
  function handleLogout(): void {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* 侧边栏 */}
      <aside
        className="flex w-56 shrink-0 flex-col border-r border-slate-200 bg-white
                   dark:border-slate-800 dark:bg-slate-900"
      >
        <div className="px-5 py-5">
          <span className="text-lg font-bold tracking-tight">EchoDesk</span>
          <p className="text-xs text-slate-500 dark:text-slate-400">长会话知识工作台</p>
        </div>

        <nav className="flex-1 space-y-1 px-3">
          {NAV_ITEMS.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              className={({ isActive }) =>
                "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition " +
                (isActive
                  ? "bg-sky-50 font-medium text-sky-700 dark:bg-sky-950 dark:text-sky-400"
                  : "text-slate-600 hover:bg-slate-100 dark:text-slate-300 dark:hover:bg-slate-800")
              }
            >
              <span aria-hidden>{item.icon}</span>
              {item.label}
            </NavLink>
          ))}
        </nav>

        {/* 用户区 */}
        <div className="border-t border-slate-200 px-4 py-3 dark:border-slate-800">
          <p className="truncate text-sm font-medium">
            {user?.display_name || user?.username || "未登录"}
          </p>
          <button
            type="button"
            onClick={handleLogout}
            className="mt-1 text-xs text-slate-500 transition hover:text-red-600"
          >
            退出登录
          </button>
        </div>
      </aside>

      {/* 主区域 */}
      <main className="flex-1 overflow-auto">
        <Outlet />
      </main>
    </div>
  );
}
