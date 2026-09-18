# ============================================================
# EchoDesk Memory Pipe —— Open WebUI 管道函数（M-08 Open WebUI 对接）
#
# 作用：把 Open WebUI 的对话转发给 memory-service /v1/chat/completions，
#       由 EchoDesk 统一持久化会话、维护 Working Memory 多轮上下文。
#
# 安装（详见 docs/04-本地环境运行手册.md §9）：
#   1. Open WebUI 管理面板 → 函数(Functions) → 新建 → 粘贴本文件全部代码
#   2. 在函数的阀门(Valves)中填写 ECHODESK_PASSWORD（服务账号 openwebui_pipe 的密码，
#      记录在 deploy\.env 的 ECHODESK_PIPE_PASSWORD）
#   3. 启用函数 → 模型列表出现 "EchoDesk Memory" → 选中即可对话
#
# 设计说明（M1 直通阶段）：
#   - 只转发本轮最新 user 消息，历史上下文由 EchoDesk Working Memory 权威维护
#     （避免 Open WebUI 本地历史与 EchoDesk 历史双重计入上下文）
#   - chat_id → EchoDesk session_id 映射存进程内存，Open WebUI 重启后旧对话将
#     开新会话（M1 已知限制，后续里程碑以标题匹配或专用映射接口优化）
#   - 所有 Open WebUI 用户共用服务账号 openwebui_pipe 的个人 workspace（M1 单租户
#     直通；按 Open WebUI 用户映射 EchoDesk 账户属于后续集成工作）
# ============================================================
"""
title: EchoDesk Memory
author: EchoDesk
version: 0.1.0
required_open_webui_version: 0.5.0
"""

import json
from typing import Any, AsyncGenerator

import httpx
from pydantic import BaseModel


class Pipe:
    """Open WebUI 管道：对话转发 EchoDesk memory-service（带流式与令牌自愈）。"""

    class Valves(BaseModel):
        """管理面板可配置项。"""

        MEMORY_BASE_URL: str = "http://memory-service:8100"
        ECHODESK_USERNAME: str = "openwebui_pipe"
        ECHODESK_PASSWORD: str = ""

    def __init__(self) -> None:
        """初始化管道状态：令牌缓存与 chat_id → session_id 映射。"""
        self.valves = self.Valves()
        self._token: str | None = None
        self._workspace_id: str | None = None
        self._sessions: dict[str, str] = {}

    # ---------- 内部工具 ----------

    async def _login(self) -> None:
        """登录 EchoDesk 获取 access token 与个人 workspace id。

        Raises:
            RuntimeError: 账号密码错误或服务不可达。
        """
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{self.valves.MEMORY_BASE_URL}/api/auth/login",
                json={
                    "username": self.valves.ECHODESK_USERNAME,
                    "password": self.valves.ECHODESK_PASSWORD,
                },
            )
            if resp.status_code != 200:
                raise RuntimeError(f"EchoDesk 登录失败 HTTP {resp.status_code}")
            self._token = resp.json()["access_token"]
            ws_resp = await client.get(
                f"{self.valves.MEMORY_BASE_URL}/api/workspaces",
                headers={"Authorization": f"Bearer {self._token}"},
            )
            ws_resp.raise_for_status()
            self._workspace_id = ws_resp.json()[0]["id"]

    async def _post_chat(self, content: str, session_id: str | None) -> httpx.Response:
        """发起一次流式 chat 请求（调用前需已登录）。

        Args:
            content: 本轮用户消息文本。
            session_id: 已有会话 id；None 表示让 EchoDesk 新建会话。
        """
        metadata: dict[str, Any] = {"workspace_id": self._workspace_id}
        if session_id:
            metadata["session_id"] = session_id
        # httpx 流式响应：read 超时给足（LLM 生成耗时），connect 快速失败
        timeout = httpx.Timeout(connect=10, read=180, write=30, pool=10)
        client = httpx.AsyncClient(timeout=timeout)
        request = client.build_request(
            "POST",
            f"{self.valves.MEMORY_BASE_URL}/v1/chat/completions",
            headers={"Authorization": f"Bearer {self._token}"},
            json={
                "stream": True,
                "metadata": metadata,
                "messages": [{"role": "user", "content": content}],
            },
        )
        return await client.send(request, stream=True)

    # ---------- Open WebUI 入口 ----------

    async def pipe(self, body: dict, __user__: dict) -> AsyncGenerator[str, None]:
        """管道主入口：转发本轮 user 消息并流式回传 EchoDesk 回答。

        Args:
            body: Open WebUI 请求体（messages 历史、chat_id 等）。
            __user__: Open WebUI 当前用户信息（M1 未用，预留多租户映射）。

        Yields:
            回答文本增量分片。
        """
        # 只取最新一条 user 消息：历史由 EchoDesk Working Memory 权威维护
        user_messages = [m for m in body.get("messages", []) if m.get("role") == "user"]
        if not user_messages or self.valves.ECHODESK_PASSWORD == "":
            yield "[EchoDesk Pipe] 配置缺失或消息为空：请在函数阀门中填写服务账号密码。"
            return
        content = user_messages[-1].get("content", "")
        if not isinstance(content, str):  # 兼容 content 为分段列表的形态
            content = "".join(
                seg.get("text", "") if isinstance(seg, dict) else str(seg)
                for seg in content
            )
        chat_id = str(body.get("chat_id", ""))
        session_id = self._sessions.get(chat_id)

        if self._token is None:
            await self._login()

        resp = await self._post_chat(content, session_id)
        if resp.status_code == 401:  # 令牌过期自愈：重登一次
            await resp.aclose()
            await self._login()
            resp = await self._post_chat(content, session_id)
        if resp.status_code != 200:
            detail = resp.text
            await resp.aclose()
            yield f"[EchoDesk Pipe] 上游错误 HTTP {resp.status_code}: {detail}"
            return

        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                payload = line[len("data: ") :]
                chunk = json.loads(payload)
                if "error" in chunk:  # 流中降级事件（上游 LLM 失败）
                    yield f"[EchoDesk Pipe] {chunk['error'].get('message', '上游错误')}"
                    continue
                sid = chunk.get("metadata", {}).get("session_id")
                if sid:
                    self._sessions[chat_id] = sid
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta
        finally:
            await resp.aclose()
