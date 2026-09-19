"""Agent 图（M-08 M3）单测：脚本化 LLM 驱动 LangGraph 全链路（docs/06 §10）。

覆盖七条链路：
  1. 无工具对话：一轮收敛 → 回答/用量落库、on_delta 推流
  2. write 工具循环：todo.create 直执行 → 审计 SUCCESS + tasks 落库 +
     role=tool 结果回灌 → 第二轮收敛、用量按多轮累加
  3. external 确认流（approve）：generate 落 PENDING 审计 + tool_call 事件 →
     interrupt 挂起 → 新会话 Command(resume) 恢复 → handler 执行 +
     SUCCESS 审计 + approver 记录
  4. external 确认流（deny）：拒绝 → DENIED 审计、handler 不执行
  5. 迭代防线：连续 8 轮工具调用后第 9 轮强制走 stream_chat 收敛
  6. 未注册工具：降级为失败文本回灌，不炸图、不产生审计行
  7. enable_tools=False：ctx.tools 为空时纯文本路径（不进工具协议）

测试用 InMemorySaver 编译图（interrupt/resume 同进程闭环，不依赖 PG
checkpoint 表）；业务落库走 conftest 的 workbench_test 真实库。
"""

import asyncio
import sys
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.graph import build_graph, initial_state
from app.agent.nodes import CTX_KEY, MAX_TOOL_ITERATIONS
from app.agent.state import GraphContext
from app.core.deps import get_redis
from app.db.session import get_session_factory
from app.llm.client import LLMUsage, StreamEvent, StreamTurn, ToolCallRequest
from app.memory.working_memory import WorkingMemory
from app.models.enums import MemberRole, MessageRole, ToolCallStatus, ToolRiskLevel
from app.models.observability import TokenUsage, ToolCallLog
from app.models.session import Message
from app.models.task import Task
from app.models.user import User
from app.repositories import user_repo, workspace_repo
from app.schemas.chat import ChatCompletionRequest, ChatMessage, ChatMetadata
from app.services.chat_service import ChatService
from app.services.session_service import SessionService
from app.tools.registry import ToolDefinition, default_registry, openai_tools_schema

# external 测试工具 handler 的调用记录（approve 断言用，fixture 内清理）
_EXTERNAL_CALLS: list[dict[str, str]] = []


class _TransferArgs(BaseModel):
    """测试 external 工具的参数模型（对齐真实工具的 Pydantic 校验链路）。"""

    target: str = Field(min_length=1, description="转账目标说明")


@pytest.fixture
def external_tool() -> str:
    """注册测试专用 external 工具（随机名防跨文件撞名），返回工具名。"""
    _EXTERNAL_CALLS.clear()
    name = f"test.transfer.{uuid.uuid4().hex[:6]}"

    async def handler(db: Any, **kwargs: Any) -> dict[str, Any]:
        """记录调用参数与操作者（仅 approve 路径会到达此处）。"""
        args: _TransferArgs = kwargs["args"]
        _EXTERNAL_CALLS.append({"target": args.target, "user_id": str(kwargs["user"].id)})
        return {"transferred_to": args.target}

    default_registry.register(
        ToolDefinition(
            name=name,
            description="测试用外部转账工具（approve 后执行）",
            risk=ToolRiskLevel.EXTERNAL,
            args_model=_TransferArgs,
            handler=handler,
        )
    )
    return name


class ScriptedLLM:
    """脚本化 LLM 替身：按序消费轮次脚本并记录收到的输入。

    脚本元素为 StreamTurn（工具协议轮）或 str（收敛轮）；类型不符即断言
    失败，防止脚本顺序写错时静默通过。
    """

    def __init__(self, turns: list[Any]) -> None:
        """Args: turns: 轮次脚本（按调用顺序消费）。"""
        self.turns = list(turns)
        self.seen: list[list[dict[str, Any]]] = []  # 每轮收到的 messages
        self.protocol_calls = 0  # stream_chat_with_tools 调用次数

    async def stream_chat_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> AsyncIterator[StreamEvent]:
        """工具协议流式：产出脚本轮次（正文一段 delta + done 汇总）。"""
        self.seen.append([dict(m) for m in messages])
        self.protocol_calls += 1
        turn = self.turns.pop(0)
        assert isinstance(turn, StreamTurn), "该轮脚本应为 StreamTurn（工具协议轮）"
        if turn.content:
            yield StreamEvent(type="delta", text=turn.content)
        yield StreamEvent(type="done", turn=turn)

    async def stream_chat(self, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        """收敛流式：产出脚本中的 str 轮次（超限防线 / 纯文本路径）。"""
        self.seen.append([dict(m) for m in messages])
        text = self.turns.pop(0)
        assert isinstance(text, str), "该轮脚本应为 str（收敛轮）"
        yield text


@dataclass
class _World:
    """单个测试用例的运行环境（图 + 已准备好的业务数据 + 事件收集器）。"""

    db: AsyncSession
    user: User
    ws_id: uuid.UUID
    session_id: uuid.UUID
    user_msg_id: uuid.UUID
    graph: CompiledStateGraph
    service: ChatService
    llm: ScriptedLLM
    content: str
    deltas: list[str]
    events: list[tuple[str, dict[str, Any]]]

    def state(self) -> dict[str, Any]:
        """构造本环境对应的图初始 state（首轮 ainvoke 用）。"""
        return initial_state(user_msg_id=str(self.user_msg_id), user_content=self.content)

    def ctx(self, db: AsyncSession, user: User, tools: list[dict[str, Any]]) -> dict[str, Any]:
        """构造 ainvoke config（GraphContext 注入 + 会话级 thread_id）。

        Args:
            db: 本次运行的数据库会话（resume 用新会话模拟新请求）。
            user: 运行用户（resume 时即确认人，审计 approver_id 来源）。
            tools: OpenAI tools 参数（空列表 = 纯文本路径）。
        """
        ctx = GraphContext(
            db=db,
            ws_id=self.ws_id,
            user=user,
            session_id=self.session_id,
            llm=self.llm,  # type: ignore[arg-type]
            chat_service=self.service,
            user_msg_id=self.user_msg_id,
            tools=tools,
            on_delta=self._on_delta,
            on_event=self._on_event,
        )
        return {"configurable": {CTX_KEY: ctx, "thread_id": f"{self.ws_id}:{self.session_id}"}}

    async def _on_delta(self, text: str) -> None:
        """收集正文增量（对应生产 SSE bridge 的 delta 事件）。"""
        self.deltas.append(text)

    async def _on_event(self, name: str, data: dict[str, Any]) -> None:
        """收集结构化事件（对应生产 SSE bridge 的 tool_call 事件）。"""
        self.events.append((name, data))


async def _make_world(db: AsyncSession, llm: ScriptedLLM, content: str) -> _World:
    """准备完整运行环境：用户/workspace/会话/用户消息落库 + 编译图。

    Args:
        db: 测试数据库会话（生命周期由用例的 async with 管理）。
        llm: 脚本化 LLM 替身。
        content: 本轮用户消息正文。

    Returns:
        运行环境句柄。
    """
    service = ChatService(llm, WorkingMemory(get_redis()), extract_hook=None)
    user = await user_repo.create(
        db, username=f"agent{uuid.uuid4().hex[:8]}", password_hash="x" * 60
    )
    ws = await workspace_repo.create(db, name="agent 图测试", owner_id=user.id)
    await workspace_repo.add_member(db, ws_id=ws.id, user_id=user.id, role=MemberRole.OWNER)
    session = await SessionService().create(db, ws_id=ws.id, user=user, title="会话")
    payload = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content=content)],
        stream=False,
        metadata=ChatMetadata(workspace_id=str(ws.id), session_id=str(session.id)),
    )
    user_msg_id = await service.prepare(db, ws_id=ws.id, session_id=session.id, payload=payload)
    await db.commit()
    return _World(
        db=db,
        user=user,
        ws_id=ws.id,
        session_id=session.id,
        user_msg_id=user_msg_id,
        graph=build_graph(InMemorySaver()),
        service=service,
        llm=llm,
        content=content,
        deltas=[],
        events=[],
    )


def _answer_turn(content: str, prompt: int = 0, completion: int = 0) -> StreamTurn:
    """构造无工具调用的收敛轮次。"""
    return StreamTurn(
        content=content,
        tool_calls=[],
        usage=LLMUsage(prompt_tokens=prompt, completion_tokens=completion),
    )


def _call_turn(
    name: str,
    arguments: dict[str, Any],
    call_id: str = "call_1",
    prompt: int = 0,
    completion: int = 0,
) -> StreamTurn:
    """构造带单个工具调用的轮次（默认零用量，便于断言累加值）。"""
    return StreamTurn(
        content="",
        tool_calls=[ToolCallRequest(id=call_id, name=name, arguments=arguments)],
        usage=LLMUsage(prompt_tokens=prompt, completion_tokens=completion),
    )


def _tool_messages(seen: list[list[dict[str, Any]]], round_no: int) -> list[dict[str, Any]]:
    """取 LLM 某轮收到输入中的 role=tool 消息（回灌断言用）。"""
    return [m for m in seen[round_no] if m.get("role") == "tool"]


async def test_plain_answer_roundtrip() -> None:
    """无工具对话：一轮收敛，回答/用量落库，on_delta 全程推流。"""
    llm = ScriptedLLM([_answer_turn("直接回答。", prompt=5, completion=7)])
    async with get_session_factory()() as db:
        world = await _make_world(db, llm, content="介绍一下自己")
        config = world.ctx(db, world.user, openai_tools_schema())
        result = await world.graph.ainvoke(world.state(), config)

        assert result["answer"] == "直接回答。"
        assert result["iterations"] == 1
        assert result["tool_calls"] == []
        assert world.deltas == ["直接回答。"]
        assert world.events == []  # 无工具调用即无结构化事件

        # 用户/回答双消息落库（prepare 与 finalize 分属两个事务，时序可排序）
        msgs = (
            (
                await db.execute(
                    select(Message)
                    .where(Message.session_id == world.session_id)
                    .order_by(Message.created_at)
                )
            )
            .scalars()
            .all()
        )
        assert [m.role for m in msgs] == [MessageRole.USER, MessageRole.ASSISTANT]
        assert msgs[1].content == "直接回答。"

        # 用量按上游脚本值落库（AccumulatedUsage 鸭子类型适配 record_turn）
        usage_row = (
            (await db.execute(select(TokenUsage).where(TokenUsage.session_id == world.session_id)))
            .scalars()
            .one()
        )
        assert usage_row.prompt_tokens == 5
        assert usage_row.completion_tokens == 7


async def test_write_tool_loop_persists_and_feeds_back() -> None:
    """write 工具循环：todo.create 直执行 → 审计 + tasks 落库 + tool 消息回灌。"""
    llm = ScriptedLLM(
        [
            _call_turn("todo.create", {"title": "字节面试准备"}, prompt=5, completion=4),
            _answer_turn("已把「字节面试准备」加入待办。", prompt=3, completion=2),
        ]
    )
    async with get_session_factory()() as db:
        world = await _make_world(db, llm, content="帮我把字节面试准备加到待办")
        config = world.ctx(db, world.user, openai_tools_schema())
        result = await world.graph.ainvoke(world.state(), config)

        assert result["answer"] == "已把「字节面试准备」加入待办。"
        assert result["iterations"] == 2
        assert [r["status"] for r in result["tool_calls"]] == [ToolCallStatus.SUCCESS.value]

        # tasks 表真实落库（handler 与 REST /api/tasks 同链路）
        task = (
            (await db.execute(select(Task).where(Task.workspace_id == world.ws_id))).scalars().one()
        )
        assert task.title == "字节面试准备"
        assert str(world.session_id) in [str(s) for s in task.related_session_ids]

        # write 风险自带审计：SUCCESS 且挂当前会话
        log = (
            (await db.execute(select(ToolCallLog).where(ToolCallLog.tool_name == "todo.create")))
            .scalars()
            .one()
        )
        assert log.status == ToolCallStatus.SUCCESS
        assert log.session_id == world.session_id

        # 第二轮 LLM 收到 role=tool 回灌（tool_call_id 对齐、结果含任务标题）
        tool_msgs = _tool_messages(llm.seen, 1)
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        assert "字节面试准备" in tool_msgs[0]["content"]

        # 用量为两轮累加值（5+3 / 4+2）
        usage_row = (
            (await db.execute(select(TokenUsage).where(TokenUsage.session_id == world.session_id)))
            .scalars()
            .one()
        )
        assert usage_row.prompt_tokens == 8
        assert usage_row.completion_tokens == 6


async def test_external_tool_approve_flow(external_tool: str) -> None:
    """external approve：PENDING 审计 + 事件 → interrupt 挂起 → resume 执行。"""
    llm = ScriptedLLM(
        [
            _call_turn(external_tool, {"target": "docs/"}, call_id="call_ext"),
            _answer_turn("已确认并完成转账。", prompt=2, completion=3),
        ]
    )
    async with get_session_factory()() as db:
        world = await _make_world(db, llm, content="把文档转给协作方")
        config = world.ctx(db, world.user, openai_tools_schema())
        result = await world.graph.ainvoke(world.state(), config)

        # 挂起态：无 answer、interrupt payload 指向该调用
        assert result["answer"] == ""
        interrupt_payload = result["__interrupt__"][0].value
        assert interrupt_payload["tool"] == external_tool
        assert interrupt_payload["risk"] == ToolRiskLevel.EXTERNAL.value
        assert interrupt_payload["args"] == {"target": "docs/"}
        assert len(llm.turns) == 1  # 收敛轮尚未消费

        # 副作用在 generate 完成（checkpoint 边界前）：PENDING 审计 + SSE 事件
        pending_log = (
            (await db.execute(select(ToolCallLog).where(ToolCallLog.tool_name == external_tool)))
            .scalars()
            .one()
        )
        assert pending_log.status == ToolCallStatus.PENDING
        assert interrupt_payload["call_id"] == str(pending_log.id)
        (event_name, event_data) = world.events[0]
        assert (len(world.events), event_name) == (1, "tool_call")
        assert event_data["call_id"] == str(pending_log.id)

    # 模拟 /v1/chat/resume：新请求新 DB 会话，同 thread 恢复
    async with get_session_factory()() as db2:
        config2 = world.ctx(db2, world.user, openai_tools_schema())
        resumed = await world.graph.ainvoke(
            Command(resume={"call_id": str(pending_log.id), "approve": True}), config2
        )

        assert resumed["answer"] == "已确认并完成转账。"
        assert [r["status"] for r in resumed["tool_calls"]] == [ToolCallStatus.SUCCESS.value]
        # handler 确实执行（approve 路径），操作者为确认人
        assert _EXTERNAL_CALLS == [{"target": "docs/", "user_id": str(world.user.id)}]

        # 审计推进至终态并记录 approver
        final_log = (
            (await db2.execute(select(ToolCallLog).where(ToolCallLog.id == pending_log.id)))
            .scalars()
            .one()
        )
        assert final_log.status == ToolCallStatus.SUCCESS
        assert final_log.approver_id == world.user.id


async def test_external_tool_deny_flow(external_tool: str) -> None:
    """external deny：拒绝后 handler 不执行，审计 DENIED，对话仍收敛。"""
    llm = ScriptedLLM(
        [
            _call_turn(external_tool, {"target": "docs/"}, call_id="call_ext"),
            _answer_turn("已按您的要求取消该操作。", prompt=2, completion=3),
        ]
    )
    async with get_session_factory()() as db:
        world = await _make_world(db, llm, content="把文档转给协作方")
        config = world.ctx(db, world.user, openai_tools_schema())
        await world.graph.ainvoke(world.state(), config)
        pending_log = (
            (await db.execute(select(ToolCallLog).where(ToolCallLog.tool_name == external_tool)))
            .scalars()
            .one()
        )

    async with get_session_factory()() as db2:
        config2 = world.ctx(db2, world.user, openai_tools_schema())
        resumed = await world.graph.ainvoke(
            Command(resume={"call_id": str(pending_log.id), "approve": False}), config2
        )

        assert resumed["answer"] == "已按您的要求取消该操作。"
        assert _EXTERNAL_CALLS == []  # handler 未执行

        final_log = (
            (await db2.execute(select(ToolCallLog).where(ToolCallLog.id == pending_log.id)))
            .scalars()
            .one()
        )
        assert final_log.status == ToolCallStatus.DENIED
        assert final_log.approver_id == world.user.id
        assert final_log.result_digest == "用户拒绝执行"

        # 拒绝说明回灌给 LLM（供其向用户解释）
        tool_msgs = _tool_messages(llm.seen, 1)
        assert "拒绝" in tool_msgs[0]["content"]


async def test_iteration_guard_forces_convergence() -> None:
    """迭代防线：连续 8 轮工具调用后，第 9 轮强制走 stream_chat 收敛。"""
    loop_turn = _call_turn("todo.list", {})
    llm = ScriptedLLM([loop_turn] * MAX_TOOL_ITERATIONS + ["已达调用上限，为您总结如下。"])
    async with get_session_factory()() as db:
        world = await _make_world(db, llm, content="不停列任务")
        config = world.ctx(db, world.user, openai_tools_schema())
        result = await world.graph.ainvoke(world.state(), config)

        assert result["answer"] == "已达调用上限，为您总结如下。"
        assert result["iterations"] == MAX_TOOL_ITERATIONS + 1
        assert len(result["tool_calls"]) == MAX_TOOL_ITERATIONS
        assert len(llm.seen) == MAX_TOOL_ITERATIONS + 1  # 8 轮协议 + 1 轮收敛


async def test_unregistered_tool_degrades() -> None:
    """未注册工具：失败文本回灌（不炸图），不产生审计行。"""
    llm = ScriptedLLM(
        [
            _call_turn("no.such.tool", {"x": 1}, call_id="call_ghost"),
            _answer_turn("该工具暂不可用。"),
        ]
    )
    async with get_session_factory()() as db:
        world = await _make_world(db, llm, content="调用不存在的工具")
        config = world.ctx(db, world.user, openai_tools_schema())
        result = await world.graph.ainvoke(world.state(), config)

        assert result["answer"] == "该工具暂不可用。"
        assert result["tool_calls"] == [
            {"call_id": "call_ghost", "tool": "no.such.tool", "status": ToolCallStatus.FAILED.value}
        ]
        tool_msgs = _tool_messages(llm.seen, 1)
        assert "未注册" in tool_msgs[0]["content"]

        # 只有 external 才在 generate 落审计；未注册调用无审计行
        logs = (await db.execute(select(ToolCallLog))).scalars().all()
        assert logs == []


async def test_tools_disabled_pure_chat() -> None:
    """enable_tools=False（ctx.tools 空）：纯文本路径，不进工具协议。"""
    llm = ScriptedLLM(["纯文本回答，不涉及工具。"])
    async with get_session_factory()() as db:
        world = await _make_world(db, llm, content="普通提问")
        config = world.ctx(db, world.user, [])
        result = await world.graph.ainvoke(world.state(), config)

        assert result["answer"] == "纯文本回答，不涉及工具。"
        assert result["tool_calls"] == []
        assert llm.protocol_calls == 0  # 从未走 stream_chat_with_tools


def test_agent_runtime_uses_postgres_checkpointer() -> None:
    """AgentRuntime.start：PG 可达时必须真用 PostgresSaver（防 setup 静默降级）。

    历史回归：checkpoint 连接非 autocommit 时 setup() 的 CREATE INDEX
    CONCURRENTLY 在事务块内失败 → 静默降级 InMemorySaver，中断恢复 DoD 失效。

    Windows 特例：psycopg 异步模式不支持 ProactorEventLoop，经
    asyncio.run + SelectorEventLoopPolicy 自管循环（不影响其他用例）。
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_verify_postgres_checkpointer())


async def _verify_postgres_checkpointer() -> None:
    """PostgresSaver 断言主体（供 sync 测试经 asyncio.run 执行）。"""
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from app.agent.checkpoint import AgentRuntime

    runtime = AgentRuntime()
    await runtime.start()
    try:
        assert isinstance(runtime._checkpointer, AsyncPostgresSaver)
        graph = runtime.build_graph()
        assert graph is not None
    finally:
        await runtime.close()
