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
    """进程内工具注册表（注册后不可变，线程安全由 GIL + 启动期注册保证）。"""

    def __init__(self) -> None:
        """初始化空注册表。"""
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, definition: ToolDefinition) -> None:
        """登记工具（重名直接覆盖，便于测试替身注入）。

        Args:
            definition: 工具定义。
        """
        self._tools[definition.name] = definition

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
