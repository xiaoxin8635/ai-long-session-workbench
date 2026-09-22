/**
 * 应用主布局（EchoDesk 前端 · 「宣纸书卷」古风体系）。
 *
 * 左侧纸面侧边栏（朱砂印章品牌 + 图标 chip 导航 + layoutId 弹簧毛笔滑块 + 用户区）
 * + 右侧主区域（路由切换水墨晕染转场）。氛围背景由 body::before/::after 提供。
 */
import { motion } from "framer-motion";
import {
  Brain,
  Command,
  Gauge,
  Library,
  ListChecks,
  LogOut,
  MessageSquare,
  Search,
  Settings,
  Wrench,
  type LucideIcon,
} from "lucide-react";
import { useCallback, useEffect, useState, type JSX } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useAuthStore } from "../stores/auth";
import { CommandPalette } from "./CommandPalette";

/** 侧边栏导航项定义。 */
interface NavItem {
  /** 路由路径。 */
  to: string;
  /** 展示名。 */
  label: string;
  /** lucide 图标组件。 */
  icon: LucideIcon;
}

/** 主导航清单（顺序即侧边栏展示顺序）。 */
const NAV_ITEMS: NavItem[] = [
  { to: "/", label: "对话", icon: MessageSquare },
  { to: "/memories", label: "记忆", icon: Brain },
  { to: "/knowledge", label: "知识库", icon: Library },
  { to: "/tasks", label: "任务", icon: ListChecks },
  { to: "/tools", label: "工具", icon: Wrench },
  { to: "/usage", label: "用量", icon: Gauge },
  { to: "/settings", label: "设置", icon: Settings },
];

/** 主布局壳组件。 */
export default function AppLayout(): JSX.Element {
  const navigate = useNavigate();
  const location = useLocation();
  const user = useAuthStore((s) => s.user);
  const logout = useAuthStore((s) => s.logout);

  /** ⌘K 命令面板开关。 */
  const [cmdOpen, setCmdOpen] = useState(false);
  const closeCmd = useCallback(() => setCmdOpen(false), []);

  // 全局快捷键：⌘K / Ctrl+K 开合命令面板
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setCmdOpen((v) => !v);
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  /** 用户显示名（无则回退用户名）。 */
  const displayName = user?.display_name || user?.username || "未登录";
  /** 头像首字母（取显示名首字符大写）。 */
  const initial = displayName.charAt(0).toUpperCase();

  /**
   * 登出并回到登录页。
   */
  function handleLogout(): void {
    logout();
    navigate("/login", { replace: true });
  }

  return (
    <div className="flex h-screen overflow-hidden">
      {/* ---- 玻璃侧边栏 ---- */}
      <aside className="glass z-10 flex w-60 shrink-0 flex-col border-y-0 border-l-0">
        {/* 品牌标：朱砂印章 + 宋体题名 */}
        <div className="relative flex items-center gap-3 px-5 py-5">
          <span className="seal-mark relative size-10 shrink-0 text-xl shadow-panel">
            <span className="relative z-10">憶</span>
          </span>
          <span className="min-w-0">
            <span className="text-ink block font-display text-lg font-semibold tracking-tight text-primary">
              EchoDesk
            </span>
            <span className="block truncate font-display text-[11px] tracking-[0.24em] text-muted">
              记忆 · 书卷
            </span>
          </span>
        </div>

        {/* 命令面板触发（⌘K） */}
        <div className="px-3 pb-1">
          <button
            type="button"
            onClick={() => setCmdOpen(true)}
            className="flex w-full items-center gap-2.5 rounded-xl border border-line/12 bg-surface/60 px-3 py-2 text-sm text-muted transition-colors duration-200 hover:border-accent/35 hover:bg-accent/5 hover:text-accent"
          >
            <Search className="size-4 shrink-0" strokeWidth={2} aria-hidden />
            <span className="flex-1 text-left">搜索或跳转…</span>
            <kbd className="flex shrink-0 items-center gap-0.5 rounded-md border border-line/12 bg-line/5 px-1.5 py-0.5 font-mono text-[10px] text-muted">
              <Command className="size-2.5" strokeWidth={2.4} aria-hidden />
              K
            </kbd>
          </button>
        </div>

        {/* 导航 */}
        <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-2">
          {NAV_ITEMS.map((item) => (
            <NavLink key={item.to} to={item.to} end={item.to === "/"} className="group block">
              {({ isActive }) => (
                <span
                  className={
                    "relative flex items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-colors duration-200 " +
                    (isActive ? "text-primary" : "text-secondary group-hover:text-primary")
                  }
                >
                  {/* 滑块背景（layoutId 在激活项间弹簧过渡，竹青水洗） */}
                  {isActive && (
                    <motion.span
                      layoutId="nav-active-pill"
                      transition={{ type: "spring", stiffness: 380, damping: 32 }}
                      className="absolute inset-0 rounded-xl bg-accent/12 ring-1 ring-inset ring-accent/25"
                    />
                  )}
                  {/* 激活态左侧竹青毛笔竖条 */}
                  {isActive && (
                    <motion.span
                      layoutId="nav-active-bar"
                      transition={{ type: "spring", stiffness: 380, damping: 32 }}
                      className="absolute -left-3 top-1/2 h-5 w-[3px] -translate-y-1/2 rounded-r-full bg-accent shadow-glow"
                    />
                  )}
                  <span
                    className={
                      "icon-chip relative size-7 shrink-0 " +
                      (isActive
                        ? "bg-accent/15 text-accent ring-accent/25"
                        : "text-muted group-hover:text-secondary")
                    }
                  >
                    <item.icon className="size-4" strokeWidth={2} />
                  </span>
                  <span className={"relative " + (isActive ? "font-display font-semibold" : "")}>{item.label}</span>
                </span>
              )}
            </NavLink>
          ))}
        </nav>

        {/* 用户区 */}
        <div className="border-t border-line/8 p-3">
          <div className="group flex items-center gap-3 rounded-xl px-2 py-2 transition-colors hover:bg-line/5">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-accent/15 to-accent/5 font-display text-sm font-semibold text-accent ring-1 ring-accent/25">
              {initial}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm font-medium text-primary">{displayName}</span>
              <span className="block truncate font-mono text-[10px] text-muted">
                @{user?.username ?? "guest"}
              </span>
            </span>
            <button
              type="button"
              onClick={handleLogout}
              aria-label="退出登录"
              title="退出登录"
              className="rounded-lg p-1.5 text-muted transition-[background-color,color,transform] duration-200 hover:bg-danger/10 hover:text-danger active:scale-90"
            >
              <LogOut className="size-4" strokeWidth={2} />
            </button>
          </div>
        </div>
      </aside>

      {/* ---- 主区域（路由转场） ---- */}
      <main className="relative flex-1 overflow-hidden">
        <motion.div
          key={location.pathname}
          initial={{ opacity: 0, y: 8 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.28, ease: [0.22, 1, 0.36, 1] }}
          className="h-full overflow-auto"
        >
          <Outlet />
        </motion.div>
      </main>

      {/* ---- ⌘K 命令面板 ---- */}
      <CommandPalette open={cmdOpen} onClose={closeCmd} />
    </div>
  );
}
