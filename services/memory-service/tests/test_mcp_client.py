"""MCP 外部工具接入（M-08 M3）单测：配置解析 / schema 转模型 / 结果归一 /
调用转发 / 连接失败降级。

不依赖真实 MCP server：调用链用注入的 ClientSession 替身验证；降级契约用
必拒绝端口（127.0.0.1:1）验证"单 server 失败不炸启动"。
"""

import time
from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from app.models.enums import ToolRiskLevel
from app.tools.mcp_client import (
    McpConnection,
    McpConnectionError,
    McpServerConfig,
    McpToolError,
    _args_model_from_schema,
    _result_to_dict,
    parse_server_configs,
)

# 必拒绝端口（TCP port 1 无监听，连接立即失败）
_REFUSED_URL = "http://127.0.0.1:1/mcp"


def _http_config(name: str = "refused", url: str = _REFUSED_URL) -> McpServerConfig:
    """构造指向必拒绝端口的 http server 配置。"""
    return McpServerConfig(name=name, transport="http", url=url)


class _FakeSession:
    """ClientSession 替身：返回预置结果并记录调用参数。"""

    def __init__(self, result: CallToolResult) -> None:
        """Args: result: call_tool 将返回的预置结果。"""
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        """记录并返回预置结果。"""
        self.calls.append((name, dict(arguments)))
        return self.result


# ---- 配置解析 ----


def test_parse_server_configs_empty_and_invalid() -> None:
    """空串/坏 JSON/非数组均返回空列表（不抛异常，交由启动降级）。"""
    assert parse_server_configs("") == []
    assert parse_server_configs("   ") == []
    assert parse_server_configs("not-json{") == []
    assert parse_server_configs('{"name":"x"}') == []  # 顶层必须是数组


def test_parse_server_configs_skips_bad_items() -> None:
    """坏项（缺 url / transport 非法 / name 大写）跳过，合法项保留。"""
    raw = (
        '[{"name":"good","transport":"http","url":"http://h:1/mcp"},'
        '{"name":"no-url","transport":"http"},'
        '{"name":"no-cmd","transport":"stdio"},'
        '{"name":"Bad","transport":"http","url":"http://h/mcp"},'
        '{"name":"ok-stdio","transport":"stdio","command":"npx","args":["-y","mcp"]}]'
    )
    configs = parse_server_configs(raw)
    assert [c.name for c in configs] == ["good", "ok-stdio"]
    assert configs[1].args == ["-y", "mcp"]


def test_server_config_disabled_field() -> None:
    """enabled=False 的合法配置可解析（连接阶段才跳过）。"""
    configs = parse_server_configs(
        '[{"name":"off","transport":"http","url":"http://h/mcp","enabled":false}]'
    )
    assert len(configs) == 1
    assert configs[0].enabled is False


def test_server_config_rejects_bad_name() -> None:
    """name 含大写/特殊字符直接 ValidationError。"""
    with pytest.raises(ValidationError):
        McpServerConfig(name="Bad.Name", transport="http", url="http://h/mcp")


# ---- input_schema → 参数模型 ----


def test_args_model_from_schema_none_cases() -> None:
    """无 schema / 空 properties 均为 None（无参工具，走 registry 既有语义）。"""
    assert _args_model_from_schema("mcp.s.t", None) is None
    assert _args_model_from_schema("mcp.s.t", {"type": "object", "properties": {}}) is None


def test_args_model_types_and_required() -> None:
    """标量映射 + required/optional 区分 + array/object 宽松容器。"""
    model = _args_model_from_schema(
        "mcp.srv.tool",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索词"},
                "limit": {"type": "integer"},
                "ratio": {"type": "number"},
                "flag": {"type": "boolean"},
                "tags": {"type": "array"},
                "opts": {"type": "object"},
            },
            "required": ["query"],
        },
    )
    assert model is not None
    args = model(query="abc")
    assert args.query == "abc"  # type: ignore[attr-defined]
    assert args.limit is None  # 可选字段缺省 None  # type: ignore[attr-defined]
    dumped = args.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {"query": "abc"}
    full = model(query="x", limit=3, ratio=0.5, flag=True, tags=["a"], opts={"k": 1})
    assert full.model_dump(by_alias=True) == {
        "query": "x",
        "limit": 3,
        "ratio": 0.5,
        "flag": True,
        "tags": ["a"],
        "opts": {"k": 1},
    }


def test_args_model_alias_for_illegal_names() -> None:
    """非 Python 标识符属性名经 alias 承载，构造与转发都保留原名。"""
    model = _args_model_from_schema(
        "mcp.srv.tool",
        {
            "type": "object",
            "properties": {"bad-name": {"type": "string"}},
            "required": ["bad-name"],
        },
    )
    assert model is not None
    args = model(**{"bad-name": "v"})  # 按 alias（远端原名）构造
    assert args.model_dump(by_alias=True) == {"bad-name": "v"}


# ---- 结果归一 ----


def test_result_to_dict_prefers_structured() -> None:
    """结构化结果（object）优先于文本块。"""
    result = CallToolResult(
        content=[TextContent(type="text", text="fallback")],
        structured_content={"items": [1, 2]},
    )
    assert _result_to_dict(result) == {"items": [1, 2]}


def test_result_to_dict_text_fallback_and_empty() -> None:
    """无结构化时拼接文本块；全空返回空 dict。"""
    text_only = CallToolResult(
        content=[TextContent(type="text", text="行一"), TextContent(type="text", text="行二")]
    )
    assert _result_to_dict(text_only) == {"text": "行一\n行二"}
    assert _result_to_dict(CallToolResult(content=[])) == {}


# ---- 调用转发与错误 ----


async def test_call_dispatches_and_normalizes() -> None:
    """call：参数原样转发远端名，结构化结果归一返回。"""
    connection = McpConnection(_http_config(), risk=ToolRiskLevel.EXTERNAL, call_timeout=5)
    fake = _FakeSession(CallToolResult(content=[], structured_content={"ok": True}))
    connection._session = fake  # type: ignore[assignment]  # 测试注入替身
    assert await connection.call("remote.tool", {"a": 1}) == {"ok": True}
    assert fake.calls == [("remote.tool", {"a": 1})]


async def test_call_raises_on_session_and_tool_error() -> None:
    """未连接抛 McpConnectionError；远端 is_error 抛 McpToolError（含文本）。"""
    connection = McpConnection(_http_config(), risk=ToolRiskLevel.EXTERNAL, call_timeout=5)
    with pytest.raises(McpConnectionError):
        await connection.call("remote.tool", {})

    connection._session = _FakeSession(  # type: ignore[assignment]
        CallToolResult(content=[TextContent(type="text", text="远端炸了")], is_error=True)
    )
    with pytest.raises(McpToolError, match="远端炸了"):
        await connection.call("remote.tool", {})


async def test_handler_forwards_alias_args() -> None:
    """handler：alias 参数按远端原名转发，本地 db 上下文被忽略。"""
    connection = McpConnection(_http_config(), risk=ToolRiskLevel.EXTERNAL, call_timeout=5)
    fake = _FakeSession(CallToolResult(content=[TextContent(type="text", text="done")]))
    connection._session = fake  # type: ignore[assignment]
    model = _args_model_from_schema(
        "mcp.srv.tool",
        {
            "type": "object",
            "properties": {"bad-name": {"type": "string"}},
            "required": ["bad-name"],
        },
    )
    assert model is not None
    handler = connection._make_handler("remote.tool")
    assert await handler(None, args=model(**{"bad-name": "v"})) == {"text": "done"}
    assert fake.calls == [("remote.tool", {"bad-name": "v"})]


# ---- 连接降级契约 ----


async def test_start_degrades_on_connection_refused() -> None:
    """连接被拒：start 返回空清单并回收后台 task（不抛异常、不悬挂）。"""
    connection = McpConnection(_http_config(), risk=ToolRiskLevel.EXTERNAL, call_timeout=5)
    started = time.monotonic()
    definitions = await connection.start()
    assert definitions == []
    assert time.monotonic() - started < 10  # 拒绝即时返回，未耗尽连接超时
    assert connection._session is None
    assert connection._task is None  # stop 已回收
    await connection.stop()  # 幂等
