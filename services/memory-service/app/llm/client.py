"""OpenAI 兼容 LLM 客户端封装（docs/06 §M-08）。

设计要点：
  - 直通 httpx 调用 /chat/completions（不引入 openai SDK，控制流式解析）
  - 流式：SSE 增量产出 delta 文本；非流式：整段返回 + usage
  - 超时与重试：llm_timeout_seconds 全程超时；5xx/网络错误重试 llm_max_retries 次
  - 降级：上游不可用抛 LLMError（502），未配置抛 LLMNotConfigured（503），
    均由调用方决定是否继续（回答链路直接透传给前端）
"""

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import get_settings
from app.observability import tracing

logger = logging.getLogger(__name__)


class LLMError(Exception):
    """LLM 上游调用失败（超时/5xx/协议异常），由路由层转 502。"""


class LLMNotConfigured(Exception):
    """LLM 未配置（base_url/model 缺失），由路由层转 503。"""


@dataclass(frozen=True)
class LLMUsage:
    """单次调用用量（上游未返回时为零值）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0


@dataclass(frozen=True)
class ToolCallRequest:
    """LLM 发起的一次工具调用请求（流式分片拼装完成后的成品）。

    Attributes:
        id: 调用 ID（回灌 tool 消息时的 tool_call_id）。
        name: 工具名。
        arguments: 参数 dict（上游 JSON 字符串解析后；解析失败为空 dict）。
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class StreamTurn:
    """带工具协议的一轮流式调用的最终汇总。

    Attributes:
        content: 全部正文增量拼接。
        tool_calls: LLM 请求的工具调用列表（无工具调用时为空）。
        usage: 上游 usage（未回传时为零值）。
    """

    content: str
    tool_calls: list[ToolCallRequest]
    usage: LLMUsage


@dataclass(frozen=True)
class StreamEvent:
    """流式事件：增量文本或终结汇总。

    Attributes:
        type: "delta"（正文增量，text 有效）或 "done"（本轮结束，turn 有效）。
        text: delta 文本（type="delta" 时非空）。
        turn: 终结汇总（type="done" 时非 None）。
    """

    type: str
    text: str = ""
    turn: StreamTurn | None = None


class LLMClient:
    """OpenAI 兼容 chat/completions 客户端（进程内复用，连接池由 httpx 管理）。"""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """初始化客户端。

        Args:
            base_url: OpenAI 兼容服务地址（如 https://api.deepseek.com/v1）。
            api_key: Bearer 凭证。
            model: 模型名。
            transport: httpx 传输层（测试注入 MockTransport 用，生产恒 None）。
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._transport = transport

    async def _post(self, payload: dict[str, Any]) -> httpx.Response:
        """带重试的 POST（仅对 5xx/网络错误重试；4xx 不重试）。"""
        settings = get_settings()
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for _attempt in range(settings.llm_max_retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=settings.llm_timeout_seconds, transport=self._transport
                ) as client:
                    resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code >= 500:
                    last_error = LLMError(f"上游 {resp.status_code}: {resp.text[:200]}")
                    continue
                if resp.status_code != 200:
                    raise LLMError(f"上游拒绝 {resp.status_code}: {resp.text[:200]}")
                return resp
            except httpx.HTTPError as exc:  # 超时/连接失败
                last_error = LLMError(f"网络错误: {exc}")
        raise last_error or LLMError("未知上游错误")

    async def complete(
        self, messages: list[dict[str, str]], *, json_mode: bool = False
    ) -> tuple[str, LLMUsage]:
        """非流式补全。

        Args:
            messages: OpenAI 格式消息列表。
            json_mode: True 时请求 response_format=json_object（结构化抽取用，
                要求 prompt 中已给出 JSON 结构说明）。

        Returns:
            (完整回答文本, usage)。

        Raises:
            LLMError: 上游失败（含协议解析异常）。
        """
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "stream": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        with tracing.generation(self._model, json_mode=json_mode) as obs:
            resp = await self._post(payload)
            try:
                data = resp.json()
                content = data["choices"][0]["message"]["content"] or ""
                usage = data.get("usage") or {}
                usage_obj = LLMUsage(
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                )
                # 非流式调用上游必回 usage：上报 token 明细供 Langfuse 成本面板
                # （output 截断片段，不全文复制；未配置观测时 no-op）
                obs.update(
                    usage_details={
                        "input": usage_obj.prompt_tokens,
                        "output": usage_obj.completion_tokens,
                    },
                    output=tracing.snippet(content),
                )
                return content, usage_obj
            except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
                raise LLMError(f"上游响应格式异常: {exc}") from exc

    async def stream_chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        """流式补全：逐段产出 delta 文本。

        Yields:
            各分片的文本增量。

        Raises:
            LLMError: 上游失败或流中断。
        """
        settings = get_settings()
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {"model": self._model, "messages": messages, "stream": True}

        # llm.call span（generation 类型）：TTFB（首字延迟）与产出字符数随
        # 消费统计，finally 统一上报——覆盖正常结束/客户端中断/重试耗尽全部
        # 路径（未配置观测时 no-op，零开销）
        started = time.perf_counter()
        ttfb_ms: float | None = None
        chars = 0
        last_error: Exception | None = None
        with tracing.generation(self._model) as obs:
            try:
                for attempt in range(settings.llm_max_retries + 1):
                    try:
                        async with httpx.AsyncClient(
                            timeout=settings.llm_timeout_seconds, transport=self._transport
                        ) as client:
                            async with client.stream(
                                "POST", url, headers=headers, json=payload
                            ) as resp:
                                if resp.status_code != 200:
                                    body = (await resp.aread()).decode("utf-8", "replace")
                                    raise LLMError(f"上游 {resp.status_code}: {body[:200]}")
                                async for line in resp.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    data = line[len("data:") :].strip()
                                    if data == "[DONE]":
                                        return
                                    try:
                                        chunk = json.loads(data)
                                        delta = chunk["choices"][0]["delta"].get("content")
                                    except (KeyError, IndexError, json.JSONDecodeError):
                                        continue  # 忽略 role/finish 等非内容分片
                                    if delta:
                                        if ttfb_ms is None:
                                            ttfb_ms = (time.perf_counter() - started) * 1000
                                        chars += len(delta)
                                        yield delta
                        return  # 流正常结束
                    except httpx.HTTPError as exc:
                        last_error = LLMError(f"网络错误: {exc}")
                        logger.warning("llm_stream_retry attempt=%s error=%s", attempt, exc)
                raise last_error or LLMError("未知上游错误")
            finally:
                obs.update(metadata={"stream": True, "ttfb_ms": ttfb_ms, "chars": chars})

    async def stream_chat_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> AsyncIterator[StreamEvent]:
        """流式补全（OpenAI function calling 协议）：边产出正文边拼装工具调用。

        事件流：
          - 若干 ``StreamEvent(type="delta")``（正文增量，供 SSE bridge 透传）
          - 一个 ``StreamEvent(type="done")``（StreamTurn：正文汇总 + 工具调用
            列表 + usage；tool_calls 非空表示 LLM 请求执行工具，本轮正文通常为空）

        流式工具调用分片按 OpenAI 协议拼装：``delta.tool_calls[i]`` 携带
        index/id/name 首片与 arguments 增量，收尾统一 JSON 解析（失败记空
        dict 并告警，交由上层参数校验拦截）。

        Args:
            messages: OpenAI 格式消息列表（含 role=tool 的工具回灌消息）。
            tools: OpenAI tools 参数（``[{"type": "function", "function": {...}}]``）。

        Yields:
            delta / done 事件。

        Raises:
            LLMError: 上游失败或流中断。
        """
        settings = get_settings()
        url = f"{self._base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        payload = {
            "model": self._model,
            "messages": messages,
            "stream": True,
            "tools": tools,
            # 末尾 chunk 回传 usage（choices 为空数组的专用分片）
            "stream_options": {"include_usage": True},
        }

        started = time.perf_counter()
        ttfb_ms: float | None = None
        chars = 0
        content_parts: list[str] = []
        # 工具调用分片拼装缓冲：index -> {id, name, arguments 分片列表}
        tool_buf: dict[int, dict[str, Any]] = {}
        usage_obj = LLMUsage()
        final_turn: StreamTurn | None = None
        last_error: Exception | None = None
        with tracing.generation(self._model, tools=len(tools)) as obs:
            try:
                for attempt in range(settings.llm_max_retries + 1):
                    try:
                        async with httpx.AsyncClient(
                            timeout=settings.llm_timeout_seconds, transport=self._transport
                        ) as client:
                            async with client.stream(
                                "POST", url, headers=headers, json=payload
                            ) as resp:
                                if resp.status_code != 200:
                                    body = (await resp.aread()).decode("utf-8", "replace")
                                    raise LLMError(f"上游 {resp.status_code}: {body[:200]}")
                                async for line in resp.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    data = line[len("data:") :].strip()
                                    if data == "[DONE]":
                                        break
                                    try:
                                        chunk = json.loads(data)
                                    except json.JSONDecodeError:
                                        continue
                                    # include_usage 专用分片：choices 空数组仅带 usage
                                    chunk_usage = chunk.get("usage")
                                    if chunk_usage:
                                        usage_obj = LLMUsage(
                                            prompt_tokens=int(chunk_usage.get("prompt_tokens", 0)),
                                            completion_tokens=int(
                                                chunk_usage.get("completion_tokens", 0)
                                            ),
                                        )
                                    for choice in chunk.get("choices") or []:
                                        self._consume_tool_stream_chunk(
                                            choice, content_parts, tool_buf
                                        )
                                        delta = choice.get("delta") or {}
                                        text = delta.get("content")
                                        if text:
                                            if ttfb_ms is None:
                                                ttfb_ms = (time.perf_counter() - started) * 1000
                                            chars += len(text)
                                            content_parts.append(text)
                                            yield StreamEvent(type="delta", text=text)
                        break  # 流正常结束（[DONE] 或服务端断流均视为完成）
                    except httpx.HTTPError as exc:
                        last_error = LLMError(f"网络错误: {exc}")
                        logger.warning("llm_stream_retry attempt=%s error=%s", attempt, exc)
                if last_error is not None and not content_parts and not tool_buf:
                    raise last_error
                final_turn = StreamTurn(
                    content="".join(content_parts),
                    tool_calls=self._flush_tool_buf(tool_buf),
                    usage=usage_obj,
                )
                yield StreamEvent(type="done", turn=final_turn)
            finally:
                # Langfuse span update 整体覆盖 metadata：收尾一次性上报，
                # 覆盖正常结束/上游拒绝/重试耗尽全部路径（未配置观测时 no-op）
                metadata: dict[str, Any] = {"stream": True, "ttfb_ms": ttfb_ms, "chars": chars}
                update_kwargs: dict[str, Any] = {"metadata": metadata}
                if final_turn is not None:
                    metadata["tool_calls"] = len(final_turn.tool_calls)
                    update_kwargs["output"] = tracing.snippet(final_turn.content)
                obs.update(**update_kwargs)

    @staticmethod
    def _consume_tool_stream_chunk(
        choice: dict[str, Any],
        content_parts: list[str],
        tool_buf: dict[int, dict[str, Any]],
    ) -> None:
        """消费一个流式 choice 分片：正文增量与工具调用增量分别落位。

        Args:
            choice: 上游 chunk 的单个 choice 对象。
            content_parts: 正文增量累积列表（原地追加）。
            tool_buf: 工具调用拼装缓冲（index -> 分片状态，原地更新）。
        """
        delta = choice.get("delta") or {}
        for tc in delta.get("tool_calls") or []:
            index = int(tc.get("index", 0))
            slot = tool_buf.setdefault(index, {"id": "", "name": "", "args_parts": []})
            if tc.get("id"):
                slot["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                slot["name"] = fn["name"]  # 名称通常单分片完整到达，覆盖即取最新
            if fn.get("arguments"):
                slot["args_parts"].append(fn["arguments"])

    @staticmethod
    def _flush_tool_buf(
        tool_buf: dict[int, dict[str, Any]],
    ) -> list[ToolCallRequest]:
        """拼装缓冲 → 成品工具调用列表（按 index 有序；arguments 容错解析）。

        Args:
            tool_buf: 流式拼装缓冲。

        Returns:
            工具调用请求列表。
        """
        calls: list[ToolCallRequest] = []
        for index in sorted(tool_buf):
            slot = tool_buf[index]
            raw_args = "".join(slot["args_parts"])
            try:
                arguments: dict[str, Any] = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                logger.warning(
                    "llm_tool_args_parse_failed name=%s raw=%r", slot["name"], raw_args[:200]
                )
                arguments = {}
            calls.append(
                ToolCallRequest(
                    id=slot["id"] or f"call_{index}",
                    name=slot["name"],
                    arguments=arguments,
                )
            )
        return calls


# ---- 进程级单例与依赖注入 ----

_llm_client: LLMClient | None = None
_extractor_client: LLMClient | None = None


def get_llm_client() -> LLMClient:
    """返回进程级 LLM 客户端（按 Settings 懒创建）。

    Raises:
        LLMNotConfigured: base_url / model 未配置。
    """
    global _llm_client
    if _llm_client is None:
        settings = get_settings()
        if not settings.llm_base_url or not settings.llm_model:
            raise LLMNotConfigured("LLM 未配置（MEMORY_SERVICE_LLM_BASE_URL / LLM_MODEL）")
        _llm_client = LLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "",
            model=settings.llm_model,
        )
    return _llm_client


def get_extractor_client() -> LLMClient:
    """返回记忆抽取用 LLM 客户端（docs/01：抽取用便宜的小模型）。

    extractor_model 已配置时使用该模型（同一 base_url/key），
    否则回落到主模型。

    Raises:
        LLMNotConfigured: base_url / model 未配置。
    """
    global _extractor_client
    if _extractor_client is None:
        settings = get_settings()
        if not settings.llm_base_url or not settings.llm_model:
            raise LLMNotConfigured("LLM 未配置（MEMORY_SERVICE_LLM_BASE_URL / LLM_MODEL）")
        _extractor_client = LLMClient(
            base_url=settings.llm_base_url,
            api_key=settings.llm_api_key or "",
            model=settings.extractor_model or settings.llm_model,
        )
    return _extractor_client


def reset_llm_client() -> None:
    """清空客户端缓存（配置变更/测试重置时调用）。"""
    global _llm_client, _extractor_client
    _llm_client = None
    _extractor_client = None
