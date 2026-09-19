"""LLMClient 工具协议流式扩展的单测（M-08 M3）。

用 httpx.MockTransport 模拟上游 SSE 分片，验证：
  - 纯正文流：delta 事件与 StreamTurn.content 汇总
  - 工具调用流：分片拼装（id/name 首片 + arguments 增量）与 usage 解析
  - 混合流：正文与工具调用并存
  - 容错：arguments 非法 JSON 解析为空 dict；无 id 分片回填序号 id
"""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.llm.client import LLMClient, LLMError, StreamEvent


def _sse_body(chunks: list[dict[str, Any]]) -> bytes:
    """把 chunk 列表编码为 SSE 响应体（data: 行 + 终止 [DONE]）。"""
    lines = [f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _client_with(body: bytes) -> LLMClient:
    """构造指向 MockTransport 的 LLMClient（单次固定响应）。"""
    return LLMClient(
        base_url="http://fake-llm/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)),
    )


async def _collect(gen: AsyncIterator[StreamEvent]) -> tuple[list[str], StreamEvent | None]:
    """收集事件流：返回 (delta 文本列表, done 事件)。"""
    deltas: list[str] = []
    done: StreamEvent | None = None
    async for event in gen:
        if event.type == "delta":
            deltas.append(event.text)
        else:
            done = event
    return deltas, done


_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": {"name": "todo.create", "description": "d", "parameters": {}}}
]


async def test_stream_plain_content() -> None:
    """纯正文流：逐 delta 产出，done 汇总正文与空工具列表。"""
    body = _sse_body(
        [
            {"choices": [{"index": 0, "delta": {"content": "你"}}]},
            {"choices": [{"index": 0, "delta": {"content": "好"}}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]
    )
    deltas, done = await _collect(_client_with(body).stream_chat_with_tools([], _TOOLS))
    assert deltas == ["你", "好"]
    assert done is not None and done.turn is not None
    assert done.turn.content == "你好"
    assert done.turn.tool_calls == []


async def test_stream_tool_calls_assembly() -> None:
    """工具调用流：分片 arguments 拼装 + include_usage 分片解析。"""
    body = _sse_body(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_abc",
                                    "type": "function",
                                    "function": {"name": "todo.create", "arguments": ""},
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"arguments": '{"title": "字节面试'}}
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '准备"}'}}]
                        },
                    }
                ]
            },
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 120, "completion_tokens": 18}},
        ]
    )
    deltas, done = await _collect(_client_with(body).stream_chat_with_tools([], _TOOLS))
    assert deltas == []
    assert done is not None and done.turn is not None
    assert done.turn.content == ""
    assert len(done.turn.tool_calls) == 1
    call = done.turn.tool_calls[0]
    assert call.id == "call_abc"
    assert call.name == "todo.create"
    assert call.arguments == {"title": "字节面试准备"}
    assert done.turn.usage.prompt_tokens == 120
    assert done.turn.usage.completion_tokens == 18


async def test_stream_mixed_content_and_tools() -> None:
    """混合流：正文与工具调用并存时两类事件均正确产出。"""
    body = _sse_body(
        [
            {"choices": [{"index": 0, "delta": {"content": "我先查一下"}}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "doc.read", "arguments": '{"path":'},
                                }
                            ]
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"/a.pdf"}'}}]
                        },
                    }
                ]
            },
        ]
    )
    deltas, done = await _collect(_client_with(body).stream_chat_with_tools([], _TOOLS))
    assert deltas == ["我先查一下"]
    assert done is not None and done.turn is not None
    assert done.turn.tool_calls[0].arguments == {"path": "/a.pdf"}


async def test_stream_invalid_arguments_falls_back_empty() -> None:
    """容错：arguments 非法 JSON 解析为空 dict（上层参数校验兜底拦截）。"""
    body = _sse_body(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "c1",
                                    "function": {"name": "x.y", "arguments": "{oops"},
                                }
                            ]
                        },
                    }
                ]
            },
        ]
    )
    _, done = await _collect(_client_with(body).stream_chat_with_tools([], _TOOLS))
    assert done is not None and done.turn is not None
    assert done.turn.tool_calls[0].arguments == {}


async def test_stream_missing_id_backfills_index() -> None:
    """容错：分片缺 id 时回填 call_<index>（qwen 偶发首片无 id）。"""
    body = _sse_body(
        [
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {"index": 2, "function": {"name": "a.b", "arguments": "{}"}}
                            ]
                        },
                    }
                ]
            },
        ]
    )
    _, done = await _collect(_client_with(body).stream_chat_with_tools([], _TOOLS))
    assert done is not None and done.turn is not None
    assert done.turn.tool_calls[0].id == "call_2"


async def test_stream_upstream_error_raises() -> None:
    """上游非 200：无任何产出时抛 LLMError（不吞错）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = LLMClient(
        base_url="http://fake-llm/v1",
        api_key="k",
        model="m",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(LLMError):
        async for _ in client.stream_chat_with_tools([], _TOOLS):
            pass
