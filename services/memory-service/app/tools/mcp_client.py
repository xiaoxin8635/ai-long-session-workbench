"""MCP 外部工具接入（M-08 M3，docs/01 §5.6 / docs/06 §11）：外部 server 工具动态注册。

连接模型：
  - 每个已配置 server 一条长连接：后台 task 持有 transport 与 ClientSession
    的 async context（stdio / streamable HTTP 双 transport），启动期
    list_tools 后以 ``mcp.<server>.<tool>`` 前缀注册进 default_registry
    （与 MCP tool 定义同构适配，注册表/执行器无感）
  - handler 忽略本地 db/user 上下文（仅满足统一签名），把校验后的参数
    转发到对应 session 的 call_tool，结果归一为 dict 回灌

安全与降级（D1 决策 + docs/01 §5.6 契约）：
  - MCP 工具默认 risk=external（执行前必须用户确认），Settings.mcp_tool_risk
    可整体放宽；server 配置经 Settings.mcp_servers_json（JSON 数组）注入
  - 单个 server 连接失败 / 配置损坏仅告警跳过，不阻塞启动与其他 server
    （部分降级）；MCP 工具执行异常由既有执行器降级为结果文本，不炸调用方
"""

import asyncio
import json
import keyword
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    create_model,
    model_validator,
)

from app.core.config import Settings
from app.models.enums import ToolRiskLevel
from app.tools.registry import ToolDefinition, default_registry

logger = logging.getLogger(__name__)

# MCP 工具注册名前缀（防与 builtin 工具重名；格式 mcp.<server>.<tool>）
_MCP_PREFIX = "mcp."


class McpError(RuntimeError):
    """MCP 接入基础异常（连接/调用/协议问题的统一父类）。"""


class McpConnectionError(McpError):
    """MCP server 未连接或连接已断开。"""


class McpToolError(McpError):
    """MCP 工具远端执行返回错误（is_error）。"""


class McpServerConfig(BaseModel):
    """单个 MCP server 的连接配置（经 Settings.mcp_servers_json 注入）。

    Attributes:
        name: server 标识（注册名前缀组成部分，限小写字母/数字/中划线）。
        transport: 传输方式（http = streamable HTTP / stdio = 子进程）。
        url: streamable HTTP 端点（http 必填）。
        command: stdio 启动命令（stdio 必填）。
        args: stdio 命令参数列表。
        env: stdio 子进程额外环境变量（None 时由 SDK 注入最小环境）。
        enabled: 是否启用（False 时启动期跳过）。
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    transport: str = Field(pattern="^(http|stdio)$")
    url: str | None = None
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _check_transport(self) -> "McpServerConfig":
        """校验 transport 与连接参数的匹配性。

        Raises:
            ValueError: http 缺 url / stdio 缺 command（pydantic 包装为
                ValidationError，由 parse_server_configs 统一跳过）。
        """
        if self.transport == "http" and not self.url:
            raise ValueError(f"MCP server {self.name}: http transport 需要 url")
        if self.transport == "stdio" and not self.command:
            raise ValueError(f"MCP server {self.name}: stdio transport 需要 command")
        return self


# JSON Schema 标量类型 → Python 类型（其余 object/array/未知按宽松容器接收）
_JSON_TYPE_MAP: dict[str, type[Any]] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
}


def _args_model_from_schema(tool_key: str, schema: dict[str, Any] | None) -> type[BaseModel] | None:
    """把 MCP 工具的 input_schema（JSON Schema）转为参数 Pydantic 模型。

    转换规则：标量类型直映射；array → list[Any]；object/未声明 → dict（宽松
    接收，参数合法性以远端 schema 为准）；非 Python 合法标识符的属性名用
    alias 承载（handler 转发时按原名还原）。空 schema 返回 None（无参工具，
    与 registry 既有语义一致）。

    Args:
        tool_key: 注册名（仅用于生成模型类名，保证可读与唯一）。
        schema: MCP 工具的 input_schema。

    Returns:
        动态参数模型；无参数时 None。
    """
    if not schema:
        return None
    properties = schema.get("properties") or {}
    if not properties:
        return None
    required = set(schema.get("required") or [])
    fields: dict[str, Any] = {}
    for index, (prop_name, prop_schema) in enumerate(properties.items()):
        prop_schema = prop_schema if isinstance(prop_schema, dict) else {}
        json_type = prop_schema.get("type")
        if json_type in _JSON_TYPE_MAP:
            py_type: type[Any] = _JSON_TYPE_MAP[json_type]
        elif json_type == "array":
            py_type = list[Any]
        else:
            py_type = dict[str, Any]
        description = prop_schema.get("description")
        # 非法标识符（含关键字）字段名换安全名 + alias 原名，保证远端参数不丢
        valid_name = prop_name.isidentifier() and not keyword.iskeyword(prop_name)
        safe_name = prop_name if valid_name else f"arg_{index}"
        alias = prop_name if safe_name != prop_name else None
        if prop_name in required:
            fields[safe_name] = (py_type, Field(description=description, alias=alias))
        else:
            fields[safe_name] = (
                py_type | None,
                Field(default=None, description=description, alias=alias),
            )
    parts = [part.capitalize() for part in re.split(r"[^0-9a-zA-Z]", tool_key) if part]
    model_name = "Mcp" + "".join(parts)
    return create_model(
        model_name,
        __config__=ConfigDict(populate_by_name=True),
        **fields,  # type: ignore[arg-type]
    )


def _result_to_dict(result: CallToolResult) -> dict[str, Any]:
    """把 MCP 调用结果归一为 dict（回灌/审计统一形态）。

    优先取结构化结果（object），否则拼接文本块；两者皆缺时返回空 dict。

    Args:
        result: MCP call_tool 结果。

    Returns:
        归一化的结果 dict。
    """
    structured = result.structured_content
    if isinstance(structured, dict) and structured:
        return structured
    texts = [block.text for block in result.content if isinstance(block, TextContent)]
    if texts:
        return {"text": "\n".join(texts)}
    if structured is not None:
        return {"result": structured}
    return {}


class McpConnection:
    """单个 MCP server 的长连接：后台 task 持有 transport/session context。

    生命周期：start() 起后台 task 并等待 initialize 完成（带连接超时）→
    工具注册期与调用期 session 保持可用 → stop() 唤醒/取消后台 task 释放
    transport。连接中断（session 置 None）后 call() 抛 McpConnectionError。
    """

    def __init__(
        self,
        config: McpServerConfig,
        *,
        risk: ToolRiskLevel,
        call_timeout: float,
        connect_timeout: float = 10.0,
    ) -> None:
        """保存连接参数并初始化同步原语。

        Args:
            config: server 配置。
            risk: 该 server 工具的统一风险分级。
            call_timeout: 单次工具调用超时（秒，ClientSession 读超时）。
            connect_timeout: start() 等待 initialize 的超时（秒）。
        """
        self._config = config
        self._risk = risk
        self._call_timeout = call_timeout
        self._connect_timeout = connect_timeout
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        # 注册名 → 远端工具名（handler 转发用）
        self._remote_names: dict[str, str] = {}

    @property
    def name(self) -> str:
        """server 标识。"""
        return self._config.name

    async def start(self) -> list[ToolDefinition]:
        """建立连接并产出可注册的工具定义清单。

        Returns:
            工具定义列表；连接失败/超时返回空列表（部分降级契约）。
        """
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(ready))
        try:
            await asyncio.wait_for(ready, timeout=self._connect_timeout)
        except Exception as exc:  # TimeoutError / _run 内连接异常统一降级
            await self.stop()
            logger.warning("mcp_server_connect_failed server=%s error=%s", self._config.name, exc)
            return []
        return await self._build_definitions()

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用远端工具并归一结果。

        Args:
            tool_name: 远端工具名（注册清单里的原始名）。
            arguments: 已校验的调用参数。

        Returns:
            归一化结果 dict。

        Raises:
            McpConnectionError: 连接不可用。
            McpToolError: 远端返回 is_error。
        """
        session = self._session
        if session is None:
            raise McpConnectionError(f"MCP server {self._config.name} 未连接")
        result = await session.call_tool(tool_name, arguments or {})
        if result.is_error:
            texts = [block.text for block in result.content if isinstance(block, TextContent)]
            raise McpToolError("\n".join(texts) or f"MCP 工具 {tool_name} 执行失败")
        return _result_to_dict(result)

    async def stop(self) -> None:
        """断开连接：唤醒后台 task 自然退出 transport，必要时取消兜底。"""
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run(self, ready: asyncio.Future[None]) -> None:
        """连接主体：按 transport 建 context，initialize 后挂起等待 stop。

        Args:
            ready: initialize 成功后置位（失败时携带异常），供 start() 等待。
        """
        try:
            if self._config.transport == "http":
                transport_ctx = streamable_http_client(self._config.url)  # type: ignore[arg-type]
            else:
                transport_ctx = stdio_client(
                    StdioServerParameters(
                        command=self._config.command,  # type: ignore[arg-type]
                        args=list(self._config.args),
                        env=self._config.env,
                    )
                )
            async with transport_ctx as (read_stream, write_stream):
                async with ClientSession(
                    read_stream, write_stream, read_timeout_seconds=self._call_timeout
                ) as session:
                    await session.initialize()
                    self._session = session
                    if not ready.done():
                        ready.set_result(None)
                    await self._stop_event.wait()  # 持住 context 直到 stop()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("mcp_connection_closed server=%s error=%s", self._config.name, exc)
            if not ready.done():
                ready.set_exception(exc)
        finally:
            self._session = None

    async def _build_definitions(self) -> list[ToolDefinition]:
        """list_tools 并转换为注册表定义（前缀命名 + 动态参数模型 + 转发 handler）。

        Returns:
            可直接 register 的工具定义列表。
        """
        session = self._session
        if session is None:
            return []
        listing = await session.list_tools()
        definitions: list[ToolDefinition] = []
        for tool in listing.tools:
            tool_key = f"{_MCP_PREFIX}{self._config.name}.{tool.name}"
            self._remote_names[tool_key] = tool.name
            definitions.append(
                ToolDefinition(
                    name=tool_key,
                    description=tool.description
                    or f"MCP 工具 {tool.name}（server: {self._config.name}）",
                    risk=self._risk,
                    args_model=_args_model_from_schema(tool_key, tool.input_schema),
                    handler=self._make_handler(tool.name),
                )
            )
        return definitions

    def _make_handler(self, remote_name: str) -> Callable[..., Awaitable[dict[str, Any]]]:
        """构造转发 handler（闭包绑定远端工具名）。

        Args:
            remote_name: 远端工具名。

        Returns:
            符合注册表统一签名的异步 handler。
        """

        async def handler(db: Any, **kwargs: Any) -> dict[str, Any]:
            """忽略本地上下文，把校验后的参数转发给远端（alias 还原原始名）。"""
            args = kwargs.get("args")
            arguments = (
                args.model_dump(by_alias=True, exclude_none=True) if args is not None else {}
            )
            return await self.call(remote_name, arguments)

        return handler


def parse_server_configs(raw: str) -> list[McpServerConfig]:
    """解析 mcp_servers_json 配置串（坏 JSON/坏项逐条跳过，部分降级）。

    Args:
        raw: JSON 数组字符串（空串视为未配置）。

    Returns:
        合法的 server 配置列表。
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("mcp_config_invalid error=%s", exc)
        return []
    if not isinstance(items, list):
        logger.warning("mcp_config_invalid error=顶层必须是 JSON 数组")
        return []
    configs: list[McpServerConfig] = []
    for item in items:
        try:
            configs.append(McpServerConfig.model_validate(item))
        except ValidationError as exc:
            logger.warning("mcp_server_config_skipped item=%s error=%s", item, exc.errors())
    return configs


# 进程级连接池（lifespan 维护；connect 幂等重建，便于测试注入配置后重连）
_CONNECTIONS: list[McpConnection] = []


async def connect_mcp_tools(settings: Settings) -> list[McpConnection]:
    """启动期接入全部已配置 MCP server 并注册工具（lifespan 调用）。

    风险分级按 Settings.mcp_tool_risk 整体放宽/收紧（默认 external，D1）；
    单个 server 失败仅告警跳过。空配置时为 no-op。

    Args:
        settings: 全局配置。

    Returns:
        建立成功的连接列表（同时登记进进程级连接池）。
    """
    _CONNECTIONS.clear()
    try:
        risk = ToolRiskLevel(settings.mcp_tool_risk)
    except ValueError:
        logger.warning("mcp_tool_risk_invalid value=%s fallback=external", settings.mcp_tool_risk)
        risk = ToolRiskLevel.EXTERNAL
    for config in parse_server_configs(settings.mcp_servers_json):
        if not config.enabled:
            continue
        connection = McpConnection(
            config,
            risk=risk,
            call_timeout=settings.mcp_call_timeout_seconds,
            connect_timeout=settings.mcp_connect_timeout_seconds,
        )
        definitions = await connection.start()
        if definitions:
            for definition in definitions:
                default_registry.register(definition)
            _CONNECTIONS.append(connection)
            logger.info(
                "mcp_server_connected server=%s tools=%d risk=%s",
                config.name,
                len(definitions),
                risk.value,
            )
        else:
            await connection.stop()
    return list(_CONNECTIONS)


async def disconnect_mcp_tools() -> None:
    """断开全部 MCP 连接（lifespan shutdown 段调用；未连接时 no-op）。"""
    for connection in _CONNECTIONS:
        await connection.stop()
    _CONNECTIONS.clear()
