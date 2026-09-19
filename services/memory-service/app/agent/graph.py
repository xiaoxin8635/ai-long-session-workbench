"""Agent 图构建（M-08 M3，docs/06 §10）：load_context → generate → tools → postprocess。

拓扑：
  START → load_context → generate ─(pending_calls 非空)→ tools → generate
                              └─(否则)────────────────→ postprocess → END

防线（docs/06 §10 LangGraph 防线）：
  - generate 迭代计数 > 8 强制收敛为文本回复（MAX_TOOL_ITERATIONS）
  - 节点异常由路由层捕获降级（LLMError → 502 / error 事件；工具异常在
    节点内降级为结果文本，不炸图）
"""

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent import nodes
from app.agent.state import GraphState


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    """编译 Agent 对话图。

    Args:
        checkpointer: checkpoint 存储（None 时编译为无持久化图——interrupt
            挂起将无法恢复，生产路径恒传入）。

    Returns:
        可 ainvoke 的编译图。
    """
    builder: StateGraph = StateGraph(GraphState)
    builder.add_node("load_context", nodes.load_context)
    builder.add_node("generate", nodes.generate)
    builder.add_node("tools", nodes.tool_node)
    builder.add_node("postprocess", nodes.postprocess)
    builder.add_edge(START, "load_context")
    builder.add_edge("load_context", "generate")
    builder.add_conditional_edges(
        "generate",
        nodes.route_after_generate,
        {"tools": "tools", "postprocess": "postprocess"},
    )
    builder.add_edge("tools", "generate")
    builder.add_edge("postprocess", END)
    return builder.compile(checkpointer=checkpointer)


def initial_state(
    *,
    user_msg_id: str,
    user_content: str,
) -> dict[str, Any]:
    """构造图初始 state（用户消息信息由路由层准备就绪后传入）。

    Args:
        user_msg_id: 本轮用户消息的数据库 ID。
        user_content: 本轮用户消息正文。

    Returns:
        GraphState 完整初始值（messages 由 load_context 节点填充）。
    """
    return {
        "user_msg_id": user_msg_id,
        "user_content": user_content,
        "messages": [],
        "answer": "",
        "tool_calls": [],
        "pending_calls": [],
        "iterations": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }
