# ============================================================
# EchoDesk Memory Pipe —— Open WebUI 管道函数（M-08 Open WebUI 对接）
#
# 作用：把 Open WebUI 的对话转发给 memory-service /v1/chat/completions，
#       由 EchoDesk 统一持久化会话、维护 Working Memory 多轮上下文；
#       M-08 M3 起支持 Agent 工具循环的 external 确认流（tool_call 事件
#       转确认提示，用户回复确认词后经 /v1/chat/resume 恢复续传）。
#
# 安装（详见 docs/04-本地环境运行手册.md §9）：
#   1. Open WebUI 管理面板 → 函数(Functions) → 新建 → 粘贴本文件全部代码
#   2. 在函数的阀门(Valves)中填写 ECHODESK_PASSWORD（服务账号 openwebui_pipe 的密码，
#      记录在 deploy\.env 的 ECHODESK_PIPE_PASSWORD）
#   3. 启用函数 → 模型列表出现 "EchoDesk Memory" → 选中即可对话
#
# 设计说明：
#   - 只转发本轮最新 user 消息，历史上下文由 EchoDesk Working Memory 权威维护
#     （避免 Open WebUI 本地历史与 EchoDesk 历史双重计入上下文）
#   - chat_id → EchoDesk session_id 映射存进程内存，Open WebUI 重启后旧对话将
#     开新会话（已知限制，后续里程碑以标题匹配或专用映射接口优化）
#   - 所有 Open WebUI 用户共用服务账号 openwebui_pipe 的个人 workspace（单租户
#     直通；按 Open WebUI 用户映射 EchoDesk 账户属于后续集成工作）
#   - external 工具确认：SSE tool_call 事件转为确认提示；确认词完全匹配
#     （防正常对话误触发）才触发 resume；发其它消息视为放弃确认，旧调用
#     保持 PENDING 终态不执行（安全侧失效）
# ============================================================
"""
title: EchoDesk Memory
author: EchoDesk
version: 0.3.0
required_open_webui_version: 0.5.0
"""

import json
from typing import Any, AsyncGenerator

import httpx
from pydantic import BaseModel

# external 确认词表（strip + lower 后完全匹配才触发，避免正常对话误判）
_APPROVE_WORDS = {"同意", "批准", "确认执行", "approve", "yes"}
_DENY_WORDS = {"拒绝", "不同意", "取消执行", "deny", "no"}


def _last_user_content(body: dict) -> str:
    """提取最新一条 user 消息文本。

    Args:
        body: Open WebUI 请求体（messages 历史）。

    Returns:
        消息文本；content 为分段列表时拼接 text 段，无 user 消息返回空串。
    """
    user_messages = [m for m in body.get("messages", []) if m.get("role") == "user"]
    if not user_messages:
        return ""
    content = user_messages[-1].get("content", "")
    if not isinstance(content, str):  # 兼容 content 为分段列表的形态
        content = "".join(
            seg.get("text", "") if isinstance(seg, dict) else str(seg) for seg in content
        )
    return content


class Pipe:
    """Open WebUI 管道：对话转发 EchoDesk memory-service（流式 + 工具确认流）。"""

    class Valves(BaseModel):
        """管理面板可配置项。"""

        MEMORY_BASE_URL: str = "http://memory-service:8100"
        ECHODESK_USERNAME: str = "openwebui_pipe"
        ECHODESK_PASSWORD: str = ""

    def __init__(self) -> None:
        """初始化管道状态：令牌、会话映射与挂起的工具确认。"""
        self.valves = self.Valves()
        self._token: str | None = None
        self._workspace_id: str | None = None
        self._sessions: dict[str, str] = {}
        # chat_key → 挂起的确认请求（{call_id, tool, session_id}）
        self._pending: dict[str, dict[str, str]] = {}

    # ---------- 内部工具 ----------

    def _auth_header(self) -> dict[str, str]:
        """构造 Bearer 认证头。"""
        return {"Authorization": f"Bearer {self._token}"}

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

    async def _open_stream(
        self, path: str, payload: dict[str, Any]
    ) -> tuple[httpx.AsyncClient, httpx.Response]:
        """发起流式 POST（含 401 自愈重登一次）。

        Args:
            path: 端点路径（/v1/chat/completions 或 /v1/chat/resume）。
            payload: 请求体。

        Returns:
            (client, resp) 元组——两者都须由调用方关闭（流式响应持有连接）。
        """
        # httpx 流式响应：read 超时给足（LLM 生成耗时），connect 快速失败
        timeout = httpx.Timeout(connect=10, read=180, write=30, pool=10)
        client = httpx.AsyncClient(timeout=timeout)
        request = client.build_request(
            "POST",
            f"{self.valves.MEMORY_BASE_URL}{path}",
            headers=self._auth_header(),
            json=payload,
        )
        resp = await client.send(request, stream=True)
        if resp.status_code == 401:  # 令牌过期自愈：重登一次
            await resp.aclose()
            await self._login()
            request = client.build_request(
                "POST",
                f"{self.valves.MEMORY_BASE_URL}{path}",
                headers=self._auth_header(),
                json=payload,
            )
            resp = await client.send(request, stream=True)
        return client, resp

    async def _post_task(self, messages: list[dict[str, str]]) -> str:
        """调用无状态任务端点（不落库），返回回答文本。

        Open WebUI 的标题/跟进生成等任务调用走此路径，避免任务 prompt
        混入用户会话历史污染 Working Memory。

        Args:
            messages: 任务 prompt 消息列表（原样透传）。

        Returns:
            回答文本；上游异常时返回空字符串（任务失败不阻塞主对话）。
        """
        url = f"{self.valves.MEMORY_BASE_URL}/v1/tasks/completions"
        payload = {"metadata": {"workspace_id": self._workspace_id}, "messages": messages}
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url, headers=self._auth_header(), json=payload)
            if resp.status_code == 401:  # 令牌过期自愈：重登一次
                await self._login()
                resp = await client.post(url, headers=self._auth_header(), json=payload)
            if resp.status_code != 200:
                print(
                    f"[echodesk-pipe] 任务端点错误 HTTP {resp.status_code}: {resp.text}",
                    flush=True,
                )
                return ""
            return resp.json()["choices"][0]["message"]["content"]

    async def _consume_stream(
        self, client: httpx.AsyncClient, resp: httpx.Response, chat_key: str
    ) -> AsyncGenerator[str, None]:
        """统一消费 EchoDesk SSE：delta 转增量、tool_call 转确认提示、error 转文案。

        副作用：更新 chat_key → session_id 映射与挂起确认记录（_pending）。

        Args:
            client: 流式请求的客户端（finally 中与响应一起关闭）。
            resp: 流式响应。
            chat_key: Open WebUI 对话 id。

        Yields:
            面向用户的文本增量（含确认提示引用块）。
        """
        try:
            async for line in resp.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = json.loads(line[len("data: ") :])
                if "error" in chunk:  # 流中降级事件（上游 LLM 失败等）
                    yield f"[EchoDesk Pipe] {chunk['error'].get('message', '上游错误')}"
                    continue
                meta = chunk.get("metadata", {})
                sid = meta.get("session_id")
                if sid:
                    self._sessions[chat_key] = sid
                tool_call = meta.get("tool_call")  # external 确认请求（M-08 M3）
                if tool_call:
                    tool = str(tool_call.get("tool", ""))
                    args_text = json.dumps(tool_call.get("args", {}), ensure_ascii=False)
                    self._pending[chat_key] = {
                        "call_id": str(tool_call.get("call_id", "")),
                        "tool": tool,
                        "session_id": self._sessions.get(chat_key, ""),
                    }
                    yield (
                        f"\n\n> ⚠️ **外部工具确认**：AI 请求执行 `{tool}`，"
                        f"参数 `{args_text}`。\n>\n> 回复 **同意** 执行 / **拒绝** 放弃。\n\n"
                    )
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content")
                if delta:
                    yield delta
        finally:
            await resp.aclose()
            await client.aclose()

    # ---------- Open WebUI 入口 ----------

    async def pipe(
        self,
        body: dict,
        __user__: dict,
        __task__: str | None = None,
        __chat_id__: str | None = None,
    ) -> Any:
        """管道主入口：任务调用走无状态端点；确认词命中走 resume；其余走主对话。

        Args:
            body: Open WebUI 请求体（messages 历史、chat_id 等）。
            __user__: Open WebUI 当前用户信息（未用，预留多租户映射）。
            __task__: Open WebUI 注入的任务标记（title_generation 等）；
                非空表示本次为无状态任务调用，不落库。
            __chat_id__: Open WebUI 注入的对话 id（优先于 body 内的同名字段）。

        Returns:
            任务调用返回完整回答字符串；主对话/恢复返回流式文本 generator。
        """
        if self._token is None:
            await self._login()

        # ---- 任务调用（标题/跟进生成）：无状态端点，不污染会话 ----
        if __task__:
            print(f"[echodesk-pipe] task={__task__!r}", flush=True)
            task_messages = [
                {"role": m.get("role", "user"), "content": m.get("content", "")}
                for m in body.get("messages", [])
                if isinstance(m.get("content"), str) and m.get("content")
            ]
            if not task_messages:
                return ""
            return await self._post_task(task_messages)

        # ---- 主对话路径 ----
        chat_key = str(__chat_id__ or body.get("chat_id") or "")
        content = _last_user_content(body)
        if not content or self.valves.ECHODESK_PASSWORD == "":
            return iter(
                ["[EchoDesk Pipe] 配置缺失或消息为空：请在函数阀门中填写服务账号密码。"]
            )

        # 确认词短路：存在挂起确认且消息完全命中确认词 → 恢复对话
        pending = self._pending.get(chat_key)
        if pending is not None:
            word = content.strip().lower()
            if word in _APPROVE_WORDS:
                return self._stream_resume(pending, approve=True, chat_key=chat_key)
            if word in _DENY_WORDS:
                return self._stream_resume(pending, approve=False, chat_key=chat_key)
            # 非确认词视为放弃确认：清映射；旧调用保持 PENDING，绝不自动执行
            self._pending.pop(chat_key, None)

        return self._stream_chat(content, chat_key)

    async def _stream_chat(self, content: str, chat_key: str) -> AsyncGenerator[str, None]:
        """主对话路径：转发本轮 user 消息并流式回传 EchoDesk 回答。

        Args:
            content: 本轮用户消息文本。
            chat_key: 对话 id（chat_id → session_id 映射维度）。

        Yields:
            回答文本增量分片（可能包含工具确认提示引用块）。
        """
        metadata: dict[str, Any] = {"workspace_id": self._workspace_id}
        session_id = self._sessions.get(chat_key)
        if session_id:
            metadata["session_id"] = session_id
        print(
            f"[echodesk-pipe] chat chat_id={chat_key!r} content={content[:80]!r}",
            flush=True,
        )

        client, resp = await self._open_stream(
            "/v1/chat/completions",
            {
                "stream": True,
                "metadata": metadata,
                "messages": [{"role": "user", "content": content}],
            },
        )
        if resp.status_code != 200:
            # 流式响应必须先 aread() 才能访问内容（直接 .text 会抛异常）
            detail = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()
            await client.aclose()
            print(f"[echodesk-pipe] 上游错误 HTTP {resp.status_code}: {detail}", flush=True)
            yield f"[EchoDesk Pipe] 上游错误 HTTP {resp.status_code}: {detail}"
            return
        async for piece in self._consume_stream(client, resp, chat_key):
            yield piece

    async def _stream_resume(
        self, pending: dict[str, str], approve: bool, chat_key: str
    ) -> AsyncGenerator[str, None]:
        """恢复路径：按用户裁决调用 /v1/chat/resume 并续传剩余回答。

        Args:
            pending: 挂起确认记录（call_id/tool/session_id）。
            approve: True 执行 / False 拒绝。
            chat_key: 对话 id。

        Yields:
            裁决摘要行 + 续传回答增量（可能携带新的工具确认提示）。
        """
        print(
            f"[echodesk-pipe] resume chat_id={chat_key!r} call={pending['call_id']} "
            f"approve={approve}",
            flush=True,
        )
        self._pending.pop(chat_key, None)  # 无论成败，本次裁决即出队
        yield ("✅ 已执行" if approve else "🚫 已拒绝") + f"：`{pending['tool']}`\n\n"

        client, resp = await self._open_stream(
            "/v1/chat/resume",
            {
                "workspace_id": self._workspace_id,
                "session_id": pending["session_id"],
                "call_id": pending["call_id"],
                "approve": approve,
            },
        )
        if resp.status_code != 200:
            detail = (await resp.aread()).decode("utf-8", errors="replace")
            await resp.aclose()
            await client.aclose()
            print(f"[echodesk-pipe] 恢复失败 HTTP {resp.status_code}: {detail}", flush=True)
            yield f"[EchoDesk Pipe] 恢复失败 HTTP {resp.status_code}: {detail}"
            return
        async for piece in self._consume_stream(client, resp, chat_key):
            yield piece
