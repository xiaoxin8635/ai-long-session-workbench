/**
 * 应用入口（EchoDesk 前端）：自托管字体 + 挂载 React 根节点。
 *
 * 字体三层（「宣纸书卷」体系）：Noto Serif SC 古风标题 /
 * Noto Sans SC 正文 / JetBrains Mono 数据与 key。经 @fontsource 自托管，
 * 不依赖外部 CDN，离线与内网可用。
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/noto-serif-sc";
import "@fontsource-variable/noto-sans-sc";
import "@fontsource-variable/jetbrains-mono";
import App from "./App";
import "./index.css";

const container = document.getElementById("root");
if (container === null) {
  throw new Error("未找到 #root 挂载点");
}

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>
);
