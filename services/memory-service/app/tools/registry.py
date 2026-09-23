"""工具注册表（M-09）：名称/描述/参数 schema/风险分级/handler 的统一登记。

设计要点：
  - ToolDefinition 与 MCP tool 定义同构（name/description/input schema），
    M3 接入 MCP server 时在注册表层适配，执行器无感
  - args 用 Pydantic 模型表达：校验、OpenAPI schema 导出二合一
  - handler 签名统一 ``(db, *, ws_id, user, session_id, args)``，执行器
    全权负责权限分级与审计，handler 只做业务动作
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.models.enums import ToolRiskLevel


@dataclass(frozen=True)
class ToolDefinition:
    """单个工具的完整定义。

    Attributes:
        name: 全局唯一工具名（点分命名，如 todo.create）。
        description: 供 LLM/前端理解的能力描述。
        risk: 风险分级（read_only 自动执行 / write 执行+审计 / external 需确认）。
        args_model: 参数 Pydantic 模型（None 表示无参数）。
        handler: 业务执行函数，签名统一
            ``(db, *, ws_id, user, session_id, args) -> dict``。
    """

    name: str
    description: str
    risk: ToolRiskLevel
    args_model: type[BaseModel] | None
    handler: Callable[..., Awaitable[dict[str, Any]]]


class ToolRegistry:
    """进程内工具注册表。

    启动期注册 builtin/env-MCP 工具；运行期经 /api/mcp/servers 热插拔
    MCP 工具（register/unregister_prefix 成对）。asyncio 单线程模型下
    dict 操作天然原子，唯一约束：迭代 list() 结果期间不得穿插注销
    （FastAPI 请求内不存在该模式）。
    """

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        """登记工具（重名直接覆盖，便于测试替身注入）。

        Args:
            definition: 工具定义。
        """
        self._tools[definition.name] = definition

    def unregister_prefix(self, prefix: str) -> int:
        """注销名称以 prefix 开头的全部工具（MCP server 热移除用）。

        Args:
            prefix: 注册名前缀（如 ``mcp.<server>.``）。

        Returns:
            实际移除的条目数。
        """
        names = [name for name in self._tools if name.startswith(prefix)]
        for name in names:
            del self._tools[name]
        return len(names)

    def list_prefix(self, prefix: str) -> list[ToolDefinition]:
        """按前缀取已注册工具（运行态展示用）。

        Args:
            prefix: 注册名前缀。

        Returns:
            命中的定义列表（按名排序）。
        """
        return [self._tools[name] for name in sorted(self._tools) if name.startswith(prefix)]

    def get(self, name: str) -> ToolDefinition | None:
        """按名取工具。

        Args:
            name: 工具名。

        Returns:
            命中的定义；未注册为 None。
        """
        return self._tools.get(name)

    def list(self) -> list[ToolDefinition]:
        """全部已注册工具（按名排序，供 /api/tools 展示）。"""
        return [self._tools[name] for name in sorted(self._tools)]


# ---- 进程级默认注册表（builtin 包导入时填充）----

default_registry = ToolRegistry()


def openai_tools_schema(registry: ToolRegistry | None = None) -> list[dict[str, Any]]:
    """把注册表转为 OpenAI tools 参数（M-08 M3 chat 工具循环用）。

    Args:
        registry: 目标注册表；None 用进程级 default_registry。

    Returns:
        ``[{"type": "function", "function": {name/description/parameters}}]``；
        参数 schema 取自 args_model 的 JSON Schema（无参工具为空 object）。
    """
    source = registry if registry is not None else default_registry
    tools: list[dict[str, Any]] = []
    for definition in source.list():
        parameters: dict[str, Any] = (
            definition.args_model.model_json_schema()
            if definition.args_model is not None
            else {"type": "object", "properties": {}}
        )
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": definition.name,
                    "description": definition.description,
                    "parameters": parameters,
                },
            }
        )
    return tools
