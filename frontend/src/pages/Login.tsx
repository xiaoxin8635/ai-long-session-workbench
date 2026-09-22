/**
 * 登录/注册页（EchoDesk 前端 · 「宣纸书卷」古风）。
 *
 * 宣纸氛围 + 远山剪影：朱砂印章品牌 + layoutId 弹簧 Tab 切换 + 竹青主按钮。
 * 双 Tab 表单：登录（用户名+密码）/ 注册（用户名+密码+显示名+可选邮箱）。
 * 成功后跳转主界面（由路由侧 redirect 参数决定落点）。
 */
import { motion } from "framer-motion";
import { ArrowRight, LoaderCircle } from "lucide-react";
import { useState, type FormEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { errorMessage, useAuthStore } from "../stores/auth";

/** 表单共享的输入框样式（墨线下划线式 + 聚焦竹青）。 */
const inputCls = "input-underline w-full px-0.5 py-2.5 text-sm";

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
    <div className="relative flex min-h-screen items-center justify-center overflow-hidden px-4">
      {/* 远山剪影（水墨装饰，极淡） */}
      <svg
        className="pointer-events-none absolute inset-x-0 bottom-0 h-56 w-full opacity-[0.09]"
        viewBox="0 0 1200 260"
        preserveAspectRatio="none"
        aria-hidden
      >
        <path
          className="text-accent"
          fill="currentColor"
          d="M0 205 L150 118 L300 190 L452 84 L620 200 L782 126 L950 205 L1100 138 L1200 200 L1200 260 L0 260 Z"
        />
        <path
          className="text-primary"
          fill="currentColor"
          d="M0 232 L200 172 L382 226 L560 162 L760 232 L980 182 L1200 236 L1200 260 L0 260 Z"
        />
      </svg>

      <motion.div
        initial={{ opacity: 0, y: 16, scale: 0.98, filter: "blur(6px)" }}
        animate={{ opacity: 1, y: 0, scale: 1, filter: "blur(0px)" }}
        transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
        className="glass relative w-full max-w-sm overflow-hidden rounded-3xl p-8 shadow-panel-lg"
      >
        {/* 卡片顶部竹青水墨晕 */}
        <div className="pointer-events-none absolute -top-24 left-1/2 h-48 w-48 -translate-x-1/2 rounded-full bg-accent/15 blur-3xl" />

        {/* 品牌标：朱砂印章 + 宋体题名 */}
        <div className="relative mb-7 text-center">
          <span className="seal-mark relative mx-auto mb-4 flex size-14 items-center justify-center rounded-2xl text-3xl shadow-panel-lg">
            <span className="relative z-10">憶</span>
          </span>
          <h1 className="text-ink font-display text-2xl font-semibold tracking-tight text-primary">EchoDesk</h1>
          <p className="mt-1 font-display text-[12px] tracking-[0.28em] text-muted">
            记忆 · 书卷
          </p>
        </div>

        {/* 模式切换 Tab（layoutId 弹簧滑块） */}
        <div className="relative mb-6 grid grid-cols-2 rounded-xl border border-line/8 bg-base/50 p-1 text-sm">
          {(["login", "register"] as const).map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => {
                setMode(m);
                setError(null);
              }}
              className={
                "relative rounded-lg px-3 py-1.5 font-medium transition-colors " +
                (mode === m ? "text-elevated" : "text-secondary hover:text-primary")
              }
            >
              {mode === m && (
                <motion.span
                  layoutId="auth-tab-pill"
                  transition={{ type: "spring", stiffness: 400, damping: 32 }}
                  className="absolute inset-0 rounded-lg bg-gradient-to-br from-accent-bright to-accent shadow-glow"
                />
              )}
              <span className="relative">{m === "login" ? "登录" : "注册"}</span>
            </button>
          ))}
        </div>

        <form onSubmit={(e) => void handleSubmit(e)} className="space-y-3.5">
          <div>
            <label htmlFor="username" className="sr-only">
              用户名
            </label>
            <input
              id="username"
              name="username"
              autoComplete="username"
              spellCheck={false}
              className={inputCls}
              placeholder="用户名（字母/数字/_/-，≥3 位）"
              value={username}
              minLength={3}
              required
              onChange={(e) => setUsername(e.target.value)}
            />
          </div>
          <div>
            <label htmlFor="password" className="sr-only">
              密码
            </label>
            <input
              id="password"
              name="password"
              className={inputCls}
              type="password"
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              placeholder="密码（≥8 位）"
              value={password}
              minLength={8}
              required
              onChange={(e) => setPassword(e.target.value)}
            />
          </div>
          {mode === "register" && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              className="space-y-3.5 overflow-hidden"
            >
              <div>
                <label htmlFor="displayName" className="sr-only">
                  显示名
                </label>
                <input
                  id="displayName"
                  name="display_name"
                  autoComplete="nickname"
                  className={inputCls}
                  placeholder="显示名（可选）"
                  value={displayName}
                  onChange={(e) => setDisplayName(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="email" className="sr-only">
                  邮箱
                </label>
                <input
                  id="email"
                  name="email"
                  type="email"
                  inputMode="email"
                  autoComplete="email"
                  spellCheck={false}
                  className={inputCls}
                  placeholder="邮箱（可选）"
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                />
              </div>
            </motion.div>
          )}

          {error && (
            <motion.p
              role="alert"
              initial={{ opacity: 0, y: -4 }}
              animate={{ opacity: 1, y: 0 }}
              className="rounded-xl border border-danger/25 bg-danger/10 px-3 py-2 text-sm text-danger"
            >
              {error}
            </motion.p>
          )}

          <motion.button
            type="submit"
            whileTap={{ scale: 0.98 }}
            disabled={submitting}
            className="btn-primary flex w-full items-center justify-center gap-2 rounded-xl px-3 py-2.5 text-sm"
          >
            {submitting ? (
              <>
                <LoaderCircle className="size-4 animate-spin" />
                提交中…
              </>
            ) : (
              <>
                {mode === "login" ? "登录工作台" : "注册并进入"}
                <ArrowRight className="size-4" strokeWidth={2.4} />
              </>
            )}
          </motion.button>
        </form>
      </motion.div>
    </div>
  );
}
