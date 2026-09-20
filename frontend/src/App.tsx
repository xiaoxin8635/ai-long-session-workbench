/**
 * 应用路由（EchoDesk 前端）。
 *
 * /login 公开；其余路由经 RequireAuth 守卫（无令牌重定向登录页并携带 redirect 参数）。
 */
import { useEffect, type JSX } from "react";
import {
  BrowserRouter,
  Navigate,
  Outlet,
  Route,
  Routes,
  useLocation,
} from "react-router-dom";
import AppLayout from "./components/AppLayout";
import ChatPage from "./pages/ChatPage";
import KnowledgePage from "./pages/KnowledgePage";
import LoginPage from "./pages/Login";
import MemoriesPage from "./pages/MemoriesPage";
import SettingsPage from "./pages/SettingsPage";
import TasksPage from "./pages/TasksPage";
import ToolsPage from "./pages/ToolsPage";
import UsagePage from "./pages/UsagePage";
import { useAuthStore } from "./stores/auth";

/** 路由守卫：未登录重定向 /login，登录后回跳原路径。 */
function RequireAuth(): JSX.Element {
  const location = useLocation();
  const accessToken = useAuthStore((s) => s.accessToken);
  if (!accessToken) {
    const redirect = encodeURIComponent(location.pathname + location.search);
    return <Navigate to={`/login?redirect=${redirect}`} replace />;
  }
  return <Outlet />;
}

/** 应用根组件：路由表 + 启动恢复登录态。 */
export default function App(): JSX.Element {
  const bootstrap = useAuthStore((s) => s.bootstrap);
  useEffect(() => {
    void bootstrap();
  }, [bootstrap]);

  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppLayout />}>
            <Route index element={<ChatPage />} />
            <Route path="memories" element={<MemoriesPage />} />
            <Route path="knowledge" element={<KnowledgePage />} />
            <Route path="tasks" element={<TasksPage />} />
            <Route path="tools" element={<ToolsPage />} />
            <Route path="usage" element={<UsagePage />} />
            <Route path="settings" element={<SettingsPage />} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
