/**
 * Tailwind 配置（EchoDesk 前端 · 「宣纸 · 文人书卷山水」古风浅色体系）。
 *
 * 设计 token 定义在 src/index.css 的 :root（RGB 通道值）；此处把它们映射为
 * Tailwind 语义色，使 bg-surface / text-accent/10 之类写法与透明度修饰符可用。
 * 浅色宣纸为唯一形态，darkMode 保留 class 策略但页面不再挂 .dark。
 */

/** @type {import('tailwindcss').Config} */
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // 墨蓝分层背景
        base: "rgb(var(--c-base) / <alpha-value>)",
        elevated: "rgb(var(--c-elevated) / <alpha-value>)",
        surface: "rgb(var(--c-surface) / <alpha-value>)",
        surface2: "rgb(var(--c-surface2) / <alpha-value>)",
        // 发丝边框（低透明使用，如 border-line/8）
        line: "rgb(var(--c-line) / <alpha-value>)",
        // 文字三级
        primary: "rgb(var(--c-primary) / <alpha-value>)",
        secondary: "rgb(var(--c-secondary) / <alpha-value>)",
        muted: "rgb(var(--c-muted) / <alpha-value>)",
        // 品牌主色（竹青石绿）
        accent: {
          DEFAULT: "rgb(var(--c-accent) / <alpha-value>)",
          bright: "rgb(var(--c-accent-bright) / <alpha-value>)",
        },
        // 印章点缀（朱砂）
        seal: "rgb(var(--c-seal) / <alpha-value>)",
        // 语义色
        success: "rgb(var(--c-success) / <alpha-value>)",
        warning: "rgb(var(--c-warning) / <alpha-value>)",
        danger: "rgb(var(--c-danger) / <alpha-value>)",
        info: "rgb(var(--c-info) / <alpha-value>)",
        violet: "rgb(var(--c-violet) / <alpha-value>)",
        teal: "rgb(var(--c-teal) / <alpha-value>)",
      },
      fontFamily: {
        display: ['"Noto Serif SC Variable"', '"Songti SC"', '"SimSun"', "serif"],
        sans: ['"Noto Sans SC Variable"', '"PingFang SC"', '"Microsoft YaHei"', "sans-serif"],
        mono: ['"JetBrains Mono Variable"', '"SFMono-Regular"', "Consolas", "monospace"],
      },
      boxShadow: {
        panel: "var(--shadow-panel)",
        "panel-lg": "var(--shadow-panel-lg)",
        glow: "var(--glow-accent)",
        ink: "0 10px 30px -16px rgb(42 38 32 / 0.5)",
        green: "0 10px 26px -12px rgb(var(--c-accent) / 0.55)",
      },
      transitionTimingFunction: {
        brush: "cubic-bezier(0.22, 1, 0.36, 1)",
        spring: "cubic-bezier(0.34, 1.56, 0.64, 1)",
      },
      keyframes: {
        "fade-in-up": {
          "0%": { opacity: "0", transform: "translateY(10px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "fade-in": {
          "0%": { opacity: "0" },
          "100%": { opacity: "1" },
        },
        "brand-breathe": {
          "0%, 100%": { opacity: "0.55", transform: "scale(1)" },
          "50%": { opacity: "1", transform: "scale(1.06)" },
        },
        shimmer: {
          "0%": { backgroundPosition: "-200% 0" },
          "100%": { backgroundPosition: "200% 0" },
        },
        "pulse-soft": {
          "0%, 100%": { opacity: "1" },
          "50%": { opacity: "0.45" },
        },
        "ink-diffuse": {
          "0%": { opacity: "0", filter: "blur(6px)", transform: "scale(0.985)" },
          "100%": { opacity: "1", filter: "blur(0)", transform: "scale(1)" },
        },
        "seal-press": {
          "0%": { transform: "translateY(-1px) scale(1)" },
          "45%": { transform: "translateY(0) scale(0.94)" },
          "100%": { transform: "translateY(0) scale(1)" },
        },
        "scroll-unfurl": {
          "0%": { opacity: "0", transform: "scaleY(0.9) translateY(-12px)" },
          "100%": { opacity: "1", transform: "scaleY(1) translateY(0)" },
        },
        "rise-in": {
          "0%": { opacity: "0", transform: "translateY(12px)" },
          "100%": { opacity: "1", transform: "translateY(0)" },
        },
        "ink-drop": {
          "0%": { transform: "scale(0.6)", opacity: "0.7" },
          "100%": { transform: "scale(2.2)", opacity: "0" },
        },
      },
      animation: {
        "fade-in-up": "fade-in-up 0.4s cubic-bezier(0.22, 1, 0.36, 1) both",
        "fade-in": "fade-in 0.3s ease both",
        "brand-breathe": "brand-breathe 3.5s ease-in-out infinite",
        shimmer: "shimmer 2s linear infinite",
        "pulse-soft": "pulse-soft 1.6s ease-in-out infinite",
        "ink-diffuse": "ink-diffuse 0.5s cubic-bezier(0.22, 1, 0.36, 1) both",
        "seal-press": "seal-press 0.28s cubic-bezier(0.34, 1.56, 0.64, 1)",
        "scroll-unfurl": "scroll-unfurl 0.42s cubic-bezier(0.22, 1, 0.36, 1) both",
        "rise-in": "rise-in 0.42s cubic-bezier(0.22, 1, 0.36, 1) both",
        "ink-drop": "ink-drop 1.4s ease-out infinite",
      },
    },
  },
  plugins: [],
};
