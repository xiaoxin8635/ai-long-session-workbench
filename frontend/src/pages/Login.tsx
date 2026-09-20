/**
 * 登录/注册页（EchoDesk 前端）。
 *
 * 双 Tab 表单：登录（用户名+密码）/ 注册（用户名+密码+显示名+可选邮箱）。
 * 成功后跳转主界面（由路由侧 redirect 参数决定落点）。
 */
import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { errorMessage, useAuthStore } from "../stores/auth";

/** 表单共享的输入框样式（Tailwind 组件级复用）。 */
const inputCls =
  "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm outline-none " +
  "focus:border-sky-500 focus:ring-2 focus:ring-sky-200 dark:border-slate-700 dark:bg-slate-900";

/** 页面组件：登录/注册双 Tab。 */
export default function LoginPage(): JSX.Element {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const login = useAuthStore((s) => s.login);
  const registerAndLogin = useAuthStore((s) => s.registerAndLogin);

  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * 提交登录或注册表单。
   *
   * @param e - 表单提交事件。
   */
  async function handleSubmit(e: FormEvent): Promise<void> {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      if (mode === "login") {
        await login(username, password);
      } else {
        await registerAndLogin({
          username,
          password,
          display_name: displayName || undefined,
          email: email || undefined,
        });
      }
      navigate(searchParams.get("redirect") ?? "/", { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <div className="w-full max-w-sm rounded-2xl bg-white p-8 shadow-lg dark:bg-slate-900">
        <h1 className="mb-1 text-2xl font-bold tracking-tight">EchoDesk</h1>
        <p className="mb-6 text-sm text-slate-500 dark:text-slate-400">
          AI 长会话知识工作台
        </p>

        {/* 模式切换 Tab */}
        <div className="mb-6 grid grid-cols-2 rounded-lg bg-slate-100 p-1 text-sm dark:bg-slate-800">
          {(["login", "register"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => {
                setMode(m);
                setError(null);
              }}
              className={
                "rounded-md px-3 py-1.5 font-medium transition " +
                (mode === m
                  ? "bg-white shadow dark:bg-slate-700"
                  : "text-slate-500 dark:text-slate-400")
              }
            >
              {m === "login" ? "登录" : "注册"}
            </button>
          ))}
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="space-y-4">
          <input
            className={inputCls}
            placeholder="用户名（字母/数字/_/-，≥3 位）"
            value={username}
            minLength={3}
            required
            onChange={(e) => setUsername(e.target.value)}
          />
          <input
            className={inputCls}
            type="password"
            placeholder="密码（≥8 位）"
            value={password}
            minLength={8}
            required
            onChange={(e) => setPassword(e.target.value)}
          />
          {mode === "register" && (
            <>
              <input
                className={inputCls}
                placeholder="显示名（可选）"
                value={displayName}
                onChange={(e) => setDisplayName(e.target.value)}
              />
              <input
                className={inputCls}
                type="email"
                placeholder="邮箱（可选）"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </>
          )}

          {error && (
            <p className="rounded-lg bg-red-50 px-3 py-2 text-sm text-red-600 dark:bg-red-950 dark:text-red-400">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={submitting}
            className="w-full rounded-lg bg-sky-600 px-3 py-2 text-sm font-medium text-white
                       transition hover:bg-sky-500 disabled:opacity-50"
          >
            {submitting
              ? "提交中…"
              : mode === "login"
                ? "登录工作台"
                : "注册并进入"}
          </button>
        </form>
      </div>
    </div>
  );
}
