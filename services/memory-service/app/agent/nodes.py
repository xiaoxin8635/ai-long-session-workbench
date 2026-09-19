"""Agent 图节点实现（M-08 M3，docs/06 §10）。

节点统一签名 ``(state, config) -> dict``，运行依赖经
``config["configurable"]["ctx"]`` 注入（GraphContext）。

设计要点：
  - load_context：复用 M-05 ContextBuilder（记忆检索 + 预算装配），检索与
    装配一体完成，避免规格中 retrieve/assemble 两节点的重复检索
  - generate：流式生成 + OpenAI 工具协议；发现工具调用时不落 answer，
    而是把 assistant(tool_calls) 消息与待执行清单写回 state（external 的
    PENDING 审计与 SSE 事件也在此落——checkpoint 边界前完成全部副作用，
    恢复重跑 tool_node 时不会重复通知）
  - tools：read_only/write 复用 M-09 execute_tool（自带审计与异常降级）；
    external 经 LangGraph interrupt 挂起等确认（用户经 /v1/chat/resume
    恢复），approve 执行 / deny 拒绝，终态全落审计
  - postprocess：复用 ChatService.finalize（落库 + 用量 + 压缩 + 抽取）
"""

import json
import logging
import uuid
from typing import Any

from langgraph.types import RunnableConfig, interrupt

from app.agent.state import AccumulatedUsage, GraphContext, GraphState
from app.core.config import get_settings
from app.core.errors import AppError
from app.llm.client import LLMError, StreamTurn
from app.models.enums import ToolCallStatus, ToolRiskLevel
from app.observability import tracing
from app.repositories import tool_call_repo
from app.tools.registry import ToolDefinition, default_registry
from app.tools.router import digest_result, execute_tool, validate_args

logger = logging.getLogger(__name__)

# 工具循环迭代上限（docs/06 §10 LangGraph 防线）：超过强制收敛为文本回复
MAX_TOOL_ITERATIONS = 8

# 工具消息回灌内容的字符上限（防超长结果撑爆下一轮上下文）
_TOOL_MSG_MAX_CHARS = 4000

# configurable 中 GraphContext 的键名
CTX_KEY = "ctx"


def get_ctx(config: RunnableConfig) -> GraphContext:
    """从 LangGraph config 取请求级运行上下文。

    Args:
        config: LangGraph 运行配置。

    Returns:
        路由层注入的 GraphContext。

    Raises:
        RuntimeError: 未注入 ctx（编程错误，图被裸调用）。
    """
    ctx = (config.get("configurable") or {}).get(CTX_KEY)
    if ctx is None:
        raise RuntimeError("Agent 图缺少 configurable.ctx（必须经 chat 路由调用）")
    return ctx  # type: ignore[no-any-return]


async def load_context(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """装配节点：复用 ChatService.assemble（M-05 装配 + M-12 观测 span）。

    Args:
        state: 当前图状态。
        config: 运行配置（ctx 携带 chat_service 与会话身份）。

    Returns:
        messages（装配产出的 LLM 输入）写入 state。
    """
    ctx = get_ctx(config)
    assembled = await ctx.chat_service.assemble(
        ctx.db,
        ws_id=ctx.ws_id,
        user_id=ctx.user.id,
        session_id=ctx.session_id,
        query=state["user_content"],
    )
    ctx.assembled = assembled
    return {"messages": assembled.messages}


def _assistant_tool_message(turn: StreamTurn) -> dict[str, Any]:
    """把带工具调用的一轮结果转为 OpenAI assistant 消息（供回灌）。

    Args:
        turn: 流式汇总（含 tool_calls）。

    Returns:
        role=assistant 且携带 tool_calls 的消息 dict。
    """
    return {
        "role": "assistant",
        "content": turn.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(call.arguments, ensure_ascii=False),
                },
            }
            for call in turn.tool_calls
        ],
    }


async def _register_pending_calls(ctx: GraphContext, turn: StreamTurn) -> list[dict[str, Any]]:
    """为待执行工具调用生成清单；external 落 PENDING 审计并推 SSE 事件。

    PENDING 审计在本节点落库并 commit（checkpoint 边界前完成副作用）：
    工具节点被 interrupt 后恢复重跑时直接挂起等待，不会重复写审计/推事件。

    Args:
        ctx: 运行上下文。
        turn: generate 轮次汇总。

    Returns:
        待执行清单（call_key=LLM 调用 ID；external 附 db_call_id）。
    """
    pending: list[dict[str, Any]] = []
    for call in turn.tool_calls:
        entry: dict[str, Any] = {
            "call_key": call.id,
            "tool": call.name,
            "args": call.arguments,
        }
        tool = default_registry.get(call.name)
        if tool is not None and tool.risk is ToolRiskLevel.EXTERNAL:
            log = await tool_call_repo.create_log(
                ctx.db,
                ws_id=ctx.ws_id,
                session_id=ctx.session_id,
                tool_name=call.name,
                risk_level=tool.risk,
                args=call.arguments,
                status=ToolCallStatus.PENDING,
            )
            # 先提交：interrupt 恢复可能跨进程，审计行必须已持久化（M-07 竞态教训）
            await ctx.db.commit()
            entry["db_call_id"] = str(log.id)
            await ctx.on_event(
                "tool_call",
                {
                    "call_id": str(log.id),
                    "tool": call.name,
                    "args": call.arguments,
                    "risk": tool.risk.value,
                },
            )
        pending.append(entry)
    return pending


async def generate(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """生成节点：流式产出（经 on_delta 推送）；发现工具调用则转入工具循环。

    迭代防线：iterations 已达上限时改用不带 tools 的收敛生成（docs/06 §10）。

    Args:
        state: 当前图状态。
        config: 运行配置。

    Returns:
        状态更新：answer（收敛时）/ pending_calls + assistant 消息（工具轮），
        以及迭代计数与多轮用量累加。

    Raises:
        LLMError: 上游流式调用失败（由路由层转 502 / error 事件）。
    """
    ctx = get_ctx(config)
    base_delta: dict[str, Any] = {
        "iterations": state["iterations"] + 1,
        "pending_calls": [],
    }

    # ---- 防线：超限强制收敛（不带 tools 的普通流式）----
    if state["iterations"] >= MAX_TOOL_ITERATIONS or not ctx.tools:
        parts: list[str] = []
        async for delta in ctx.llm.stream_chat(state["messages"]):
            parts.append(delta)
            await ctx.on_delta(delta)
        return {
            **base_delta,
            "answer": "".join(parts),
        }

    # ---- 工具协议流式生成 ----
    parts = []
    turn: StreamTurn | None = None
    async for event in ctx.llm.stream_chat_with_tools(state["messages"], ctx.tools):
        if event.type == "delta":
            parts.append(event.text)
            await ctx.on_delta(event.text)
        else:
            turn = event.turn
    if turn is None:  # 生成器正常结束必产出 done 事件；防御协议异常
        raise LLMError("上游流式响应缺少终止汇总")
    usage = {
        "prompt": state["prompt_tokens"] + turn.usage.prompt_tokens,
        "completion": state["completion_tokens"] + turn.usage.completion_tokens,
    }

    if not turn.tool_calls:
        return {
            **base_delta,
            "answer": turn.content,
            "prompt_tokens": usage["prompt"],
            "completion_tokens": usage["completion"],
        }
    pending = await _register_pending_calls(ctx, turn)
    return {
        "messages": [*state["messages"], _assistant_tool_message(turn)],
        "pending_calls": pending,
        "iterations": state["iterations"] + 1,
        "prompt_tokens": usage["prompt"],
        "completion_tokens": usage["completion"],
    }


def route_after_generate(state: GraphState) -> str:
    """条件边：有待执行工具调用 → tools；否则 → postprocess。

    Args:
        state: generate 后的图状态。

    Returns:
        下一节点名。
    """
    return "tools" if state.get("pending_calls") else "postprocess"


def _tool_message(call_key: str, result: dict[str, Any]) -> dict[str, Any]:
    """构造工具结果回灌消息（role=tool，摘要化截断）。

    Args:
        call_key: LLM 工具调用 ID（tool_call_id）。
        result: 工具执行结果（或失败说明）。

    Returns:
        OpenAI tool 消息 dict。
    """
    content = json.dumps(result, ensure_ascii=False, default=str)
    if len(content) > _TOOL_MSG_MAX_CHARS:
        content = content[:_TOOL_MSG_MAX_CHARS] + "…[已截断]"
    return {"role": "tool", "tool_call_id": call_key, "content": content}


async def _run_external_with_decision(
    ctx: GraphContext,
    tool: ToolDefinition,
    entry: dict[str, Any],
    decision: object,
) -> tuple[dict[str, Any], str]:
    """按用户裁决执行 external 工具并落终态审计。

    裁决容错：非 dict / call_id 不匹配 / approve 非 True 一律按拒绝处理；
    记录已被并发改写（非 PENDING）时拒绝重复执行。

    Args:
        ctx: 运行上下文（db 为 resume 请求的会话；user 为确认人）。
        tool: 工具定义。
        entry: 待执行清单条目（含 db_call_id 与原始参数）。
        decision: interrupt 恢复值（{call_id, approve}）。

    Returns:
        (工具结果或失败说明, 审计状态值)。
    """
    call_id = uuid.UUID(entry["db_call_id"])
    approved = (
        isinstance(decision, dict)
        and decision.get("call_id") == entry["db_call_id"]
        and decision.get("approve") is True
    )
    log = await tool_call_repo.get_log(ctx.db, ws_id=ctx.ws_id, call_id=call_id)
    if log is None:
        return {"status": "failed", "error": "确认记录不存在"}, ToolCallStatus.FAILED.value
    if log.status != ToolCallStatus.PENDING:  # StrEnum：DB 回读为 str，必须 ==
        return {"status": "failed", "error": "该调用已被处理"}, log.status
    log.approver_id = ctx.user.id
    if not approved:
        log.status = ToolCallStatus.DENIED
        log.result_digest = "用户拒绝执行"
        await ctx.db.commit()
        return {"status": "denied", "message": "用户拒绝执行该操作"}, ToolCallStatus.DENIED.value
    try:
        args = validate_args(tool, entry["args"])
        with tracing.span(
            "tool.invoke",
            tool=tool.name,
            risk=tool.risk.value,
            workspace_id=str(ctx.ws_id),
            session_id=str(ctx.session_id),
            mode="chat_confirmed",
        ) as obs:
            result = await tool.handler(
                ctx.db, ws_id=ctx.ws_id, user=ctx.user, session_id=ctx.session_id, args=args
            )
            obs.update(metadata={"status": ToolCallStatus.SUCCESS.value})
        log.status = ToolCallStatus.SUCCESS
        log.result_digest = digest_result(result)
        await ctx.db.commit()
        return result, ToolCallStatus.SUCCESS.value
    except Exception as exc:  # 工具异常降级为结果文本，不炸图（docs/01 §5.6）
        logger.warning("agent_tool_failed tool=%s error=%s", tool.name, exc)
        log.status = ToolCallStatus.FAILED
        log.result_digest = f"error: {exc}"[: get_settings().tool_result_max_chars]
        await ctx.db.commit()
        return {"status": "failed", "error": str(exc)}, ToolCallStatus.FAILED.value


async def tool_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """工具节点：逐个执行待调用（read_only/write 直执行；external 挂起等确认）。

    external 分支的 interrupt 恢复语义：恢复时本节点从头重跑，interrupt()
    直接返回 resume 而不重复副作用（PENDING 审计与 SSE 事件已在 generate 落）。

    Args:
        state: 当前图状态（pending_calls 驱动）。
        config: 运行配置。

    Returns:
        追加的 tool 消息、已执行摘要清空 pending_calls。
    """
    ctx = get_ctx(config)
    new_msgs: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for entry in state["pending_calls"]:
        tool = default_registry.get(entry["tool"])
        if tool is None:
            result = {"status": "failed", "error": f"工具 {entry['tool']} 未注册"}
            status = ToolCallStatus.FAILED.value
        elif tool.risk is ToolRiskLevel.EXTERNAL:
            decision = interrupt(
                {
                    "call_id": entry["db_call_id"],
                    "tool": entry["tool"],
                    "args": entry["args"],
                    "risk": tool.risk.value,
                }
            )
            result, status = await _run_external_with_decision(ctx, tool, entry, decision)
        else:
            try:
                exec_result = await execute_tool(
                    ctx.db,
                    ws_id=ctx.ws_id,
                    user=ctx.user,
                    name=entry["tool"],
                    raw_args=entry["args"],
                    session_id=ctx.session_id,
                )
            except AppError as exc:
                # 参数校验失败（LLM 传错参数是常态）：回灌错误文本供自纠，
                # 不炸图——降级契约与 docs/01 §5.6 一致
                logger.warning("agent_tool_args_invalid tool=%s error=%s", entry["tool"], exc)
                result = {"status": "failed", "error": exc.detail}
                status = ToolCallStatus.FAILED.value
            else:
                status = exec_result.status.value
                if exec_result.result is not None:
                    result = exec_result.result
                else:
                    result = {"status": "failed", "error": exec_result.error or "工具执行失败"}
        records.append(
            {
                "call_id": entry.get("db_call_id") or entry["call_key"],
                "tool": entry["tool"],
                "status": status,
            }
        )
        new_msgs.append(_tool_message(entry["call_key"], result))
    return {
        "messages": [*state["messages"], *new_msgs],
        "tool_calls": [*state["tool_calls"], *records],
        "pending_calls": [],
    }


async def postprocess(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    """收尾节点：复用 ChatService.finalize（落库 + 用量 + 压缩 + 抽取）。

    resume 场景 assembled 为 None（load_context 未重跑），用量区块明细记 0，
    prompt/completion 仍按 state 累加值落库。

    Args:
        state: 当前图状态。
        config: 运行配置。

    Returns:
        answer 写回（幂等收敛输出）。
    """
    ctx = get_ctx(config)
    answer = state["answer"]
    await ctx.chat_service.finalize(
        ctx.db,
        ws_id=ctx.ws_id,
        user_id=ctx.user.id,
        session_id=ctx.session_id,
        answer=answer,
        user_msg_id=uuid.UUID(state["user_msg_id"]),
        user_content=state["user_content"],
        assembled=ctx.assembled,
        usage=AccumulatedUsage(
            prompt_tokens=state["prompt_tokens"],
            completion_tokens=state["completion_tokens"],
        ),
    )
    return {"answer": answer, "pending_calls": []}
