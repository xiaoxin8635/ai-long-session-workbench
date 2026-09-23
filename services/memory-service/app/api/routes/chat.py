"""Chat 端点：POST /v1/chat/completions + /v1/chat/resume（docs/01 §6.1 / docs/06 §M-08）。

协议要点：
  - 请求带 EchoDesk 扩展 metadata（workspace_id 必填 / session_id 可选自动建会话
    / enable_tools 工具循环开关）
  - Bearer JWT 认证 + workspace 成员校验（与 /api/* 同一套防线）
  - 主对话经 LangGraph Agent 图（M-08 M3）：装配 → 流式生成 → 工具循环（≤8 迭代）
    → 落库；external 工具经 SSE tool_call 事件请求确认并挂起，用户裁决后经
    /v1/chat/resume 恢复续传
  - SSE 事件：delta(正文) / tool_call(external 确认请求，metadata 内) /
    finish(usage+citations，含 pending_confirmation) / error(上游失败) / [DONE]
  - 非流式：标准 chat.completion JSON（挂起时 metadata.pending_confirmation）
  - LLM 未配置 → 503；上游失败 → 502（流开始后以 error 事件收尾）
  - /v1/tasks/completions：Open WebUI 标题/跟进等无状态任务，不落库纯透传
"""

import asyncio
import json
import logging
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from langgraph.types import Command

from app.agent.checkpoint import get_agent_runtime
from app.agent.nodes import CTX_KEY
from app.agent.state import GraphContext
from app.context.tokenizer import count_tokens
from app.core.config import get_settings
from app.core.deps import CurrentUser, DbDep, get_redis, require_ws_member
from app.core.errors import AppError, NotFoundError
from app.llm.client import LLMError, LLMNotConfigured, get_llm_client
from app.memory.working_memory import WorkingMemory
from app.models.enums import ToolCallStatus
from app.observability import tracing
from app.repositories import session_repo, tool_call_repo
from app.schemas.chat import (
    ChatCompletionRequest,
    ChatMetadata,
    ChatResumeRequest,
    TaskCompletionRequest,
)
from app.schemas.knowledge import Citation
from app.services.chat_service import ChatService
from app.tools.registry import openai_tools_schema

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["chat"])


def _sse(data: dict) -> str:
    """编码一条 SSE data 行。"""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _require_member(metadata: ChatMetadata, user: CurrentUser, db: DbDep) -> uuid_mod.UUID:
    """校验 metadata.workspace_id 合法且当前用户是其成员（403 防枚举）。

    Args:
        metadata: 请求携带的 EchoDesk 扩展 metadata。
        user: 当前认证用户。
        db: 数据库会话。

    Returns:
        解析后的 workspace UUID。

    Raises:
        AppError: workspace_id 非法（422）。
        PermissionDeniedError: 非成员或 workspace 不存在（403，统一不区分）。
    """
    return await require_ws_member(metadata.workspace_id, user, db)


def _citations(cited_chunks: tuple) -> list[Citation]:
    """装配结果 RAG 引用 → API 模型（M-07 metadata.citations）。"""
    return [Citation.from_cited(cited) for cited in cited_chunks]


def _parse_attachment_ids(raw: list[str] | None) -> list[uuid_mod.UUID]:
    """解析 metadata.attachment_ids（字符串 → UUID）。

    Args:
        raw: 客户端传入的附件文件 ID 列表；None/空表示本轮无附件。

    Returns:
        合法 UUID 列表（保持顺序，自动去重）。

    Raises:
        AppError: 含非法 UUID（422，客户端参数错误）。
    """
    if not raw:
        return []
    parsed: list[uuid_mod.UUID] = []
    seen: set[uuid_mod.UUID] = set()
    for item in raw:
        try:
            fid = uuid_mod.UUID(item)
        except (ValueError, AttributeError, TypeError) as exc:
            raise AppError("invalid_attachment_id", 422, f"附件 ID 非法: {item}") from exc
        if fid not in seen:
            seen.add(fid)
            parsed.append(fid)
    return parsed


def _thread_config(ws_id: uuid_mod.UUID, session_id: uuid_mod.UUID, ctx: GraphContext) -> dict:
    """构造 LangGraph 运行配置（thread_id 保证会话级 checkpoint 隔离）。

    Args:
        ws_id: workspace（thread 隔离第一维）。
        session_id: 会话（thread 隔离第二维）。
        ctx: 请求级运行上下文。

    Returns:
        ainvoke 的 config dict。
    """
    return {"configurable": {CTX_KEY: ctx, "thread_id": f"{ws_id}:{session_id}"}}


class _AgentBridge:
    """Agent 图 ↔ SSE 的桥接：asyncio.Queue 把节点回调转为可迭代的分片事件。

    事件元组约定：
      ("delta", text)             —— 正文增量
      ("tool_call", payload)      —— external 确认请求
      ("done", {"answer": ...})   —— 图正常收敛（含最终回答与引用）
      ("error", {"message": ...}) —— 图执行异常（上游失败等）
    """

    def __init__(
        self,
        db: DbDep,
        *,
        ws_id: uuid_mod.UUID,
        user: CurrentUser,
        session_id: uuid_mod.UUID,
        user_msg_id: uuid_mod.UUID,
        user_content: str,
        service: ChatService,
        llm: object,
        enable_tools: bool,
        attachment_ids: list[uuid_mod.UUID] | None = None,
    ) -> None:
        """保存执行依赖并初始化事件队列。

        Args:
            db: 请求级数据库会话。
            ws_id: 所属 workspace。
            user: 当前认证用户。
            session_id: 目标会话。
            user_msg_id: 本轮用户消息 ID（prepare 产出）。
            user_content: 本轮用户消息正文。
            service: chat 用例编排（装配与收尾复用）。
            llm: LLM 客户端。
            enable_tools: 是否注入工具参数。
            attachment_ids: 本轮附件的知识文件 ID（强制注入其切片）。
        """
        self._db = db
        self._ws_id = ws_id
        self._user = user
        self._session_id = session_id
        self._user_msg_id = user_msg_id
        self._user_content = user_content
        self._service = service
        self._llm = llm
        self._tools = openai_tools_schema() if enable_tools else []
        self._attachment_ids = attachment_ids or []
        self._queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        self._ctx: GraphContext | None = None

    async def _on_delta(self, text: str) -> None:
        """generate 节点的正文增量回调（推入队列供 SSE 消费）。"""
        await self._queue.put(("delta", text))

    async def _on_event(self, name: str, payload: dict) -> None:
        """节点结构化事件回调（当前仅 tool_call 确认请求）。"""
        await self._queue.put((name, payload))

    def _build_ctx(self) -> GraphContext:
        """构造图运行上下文（缓存实例：assembled 经 load_context 回填后可查）。

        Returns:
            GraphContext（tools/on_delta/on_event 已接桥）。
        """
        if self._ctx is None:
            self._ctx = GraphContext(
                db=self._db,
                ws_id=self._ws_id,
                user=self._user,
                session_id=self._session_id,
                llm=self._llm,  # type: ignore[arg-type]
                chat_service=self._service,
                user_msg_id=self._user_msg_id,
                tools=self._tools,
                on_delta=self._on_delta,
                on_event=self._on_event,
                attachment_ids=self._attachment_ids,
            )
        return self._ctx

    def citations(self) -> tuple:
        """本轮装配产出的 RAG 引用（load_context 前为空元组）。"""
        if self._ctx is not None and self._ctx.assembled is not None:
            return self._ctx.assembled.citations
        return ()

    async def _run(self, initial_state: dict | Command) -> None:
        """后台执行图并把终结事件入队（ainvoke 结果经 done/error 事件传出）。

        Args:
            initial_state: 新对话传初始 state；恢复传 Command(resume=...)。
        """
        graph = get_agent_runtime().build_graph()
        try:
            result = await graph.ainvoke(
                initial_state,
                config=_thread_config(self._ws_id, self._session_id, self._build_ctx()),
            )
            pending = None
            interrupts = result.get("__interrupt__") if isinstance(result, dict) else None
            if interrupts:
                # interrupt payload：{call_id, tool, args, risk}（external 确认请求）
                pending = dict(interrupts[0].value)
            await self._queue.put(
                ("done", {"answer": result.get("answer", ""), "interrupt": pending})
            )
        except Exception as exc:  # 图内异常统一转 error 事件（上游失败/协议错误）
            logger.error("agent_invoke_failed session=%s error=%s", self._session_id, exc)
            # kind 供消费方分流错误类别（Fix 轮：DB 死锁曾被一律标成
            # llm_upstream_error 502，误导排查方向）
            await self._queue.put(("error", {"message": str(exc), "kind": type(exc).__name__}))
        finally:
            await self._queue.put(None)

    async def stream(self, initial_state: dict | Command) -> AsyncIterator[tuple[str, Any]]:
        """消费图执行：逐事件产出，结束时确保后台任务收尾。

        Args:
            initial_state: 初始 state 或恢复命令。

        Yields:
            ("delta"|"tool_call"|"done"|"error", payload) 事件元组。
        """
        task = asyncio.create_task(self._run(initial_state))
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                yield item
        finally:
            if not task.done():  # 客户端断开：中止图执行（checkpoint 已保证可恢复）
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def _chunk(
    completion_id: str,
    created: int,
    delta: dict,
    *,
    metadata: dict | None = None,
    finish_reason: str | None = None,
) -> dict:
    """构造 OpenAI 格式的 SSE chunk 骨架。

    Args:
        completion_id: 补全 ID。
        created: 时间戳。
        delta: choices[0].delta 内容。
        metadata: EchoDesk 扩展 metadata（首片/finish 片携带）。
        finish_reason: 结束原因（finish 片为 "stop"）。

    Returns:
        chunk dict。
    """
    chunk: dict = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": "echodesk",
        "choices": [{"index": 0, "delta": delta}],
    }
    if finish_reason:
        chunk["choices"][0]["finish_reason"] = finish_reason
    if metadata:
        chunk["metadata"] = metadata
    return chunk


@router.post(
    "/chat/completions",
    summary="OpenAI 兼容对话补全（EchoDesk 扩展 metadata，Agent 图编排）",
    response_model=None,  # 流式返回 StreamingResponse / 非流式返回裸 JSON
)
async def chat_completions(payload: ChatCompletionRequest, user: CurrentUser, db: DbDep):
    """M3 完整版：LangGraph 图编排（装配 → 生成 → 工具循环 → 落库）。"""
    # ---- workspace 校验（body.metadata 传入，非 query）----
    ws_id = await _require_member(payload.metadata, user, db)

    try:
        llm = get_llm_client()
    except LLMNotConfigured as exc:
        raise AppError("llm_not_configured", 503, str(exc)) from exc
    service = ChatService(llm, WorkingMemory(get_redis()))

    # ---- 会话解析 + 用户消息落库（任何响应模式前完成）----
    session_id = await service.resolve_session(db, ws_id=ws_id, user=user, payload=payload)
    user_msg_id = await service.prepare(db, ws_id=ws_id, session_id=session_id, payload=payload)

    completion_id = f"chatcmpl-{uuid_mod.uuid4().hex}"
    created = int(time.time())
    query = payload.messages[-1].content
    attachment_ids = _parse_attachment_ids(payload.metadata.attachment_ids)
    bridge = _AgentBridge(
        db,
        ws_id=ws_id,
        user=user,
        session_id=session_id,
        user_msg_id=user_msg_id,
        user_content=query,
        service=service,
        llm=llm,
        enable_tools=payload.metadata.enable_tools,
        attachment_ids=attachment_ids,
    )
    state = {
        "user_msg_id": str(user_msg_id),
        "user_content": query,
        "messages": [],
        "answer": "",
        "tool_calls": [],
        "pending_calls": [],
        "iterations": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
    }

    # ---- 非流式：同步跑图（on_delta 为 no-op 消费）----
    if not payload.stream:
        with tracing.turn(
            session_id=str(session_id),
            user_id=str(user.id),
            tags=["chat"],
            workspace_id=str(ws_id),
            stream=False,
            model=get_settings().llm_model,
            query=tracing.snippet(query),
        ):
            final_answer = ""
            pending_confirmation = None
            try:
                async for kind, data in bridge.stream(state):
                    if kind == "done":
                        final_answer = data["answer"]  # type: ignore[union-attr]
                        pending_confirmation = data.get("interrupt")  # type: ignore[union-attr]
                    elif kind == "error":
                        # LLM 上游失败 → 502；其余（DB/内部异常）→ 500，类别不混淆
                        if data.get("kind") == "LLMError":
                            raise AppError("llm_upstream_error", 502, str(data["message"]))
                        raise AppError("agent_internal_error", 500, str(data["message"]))
            except LLMError as exc:
                raise AppError("llm_upstream_error", 502, str(exc)) from exc
        metadata_out: dict = {"session_id": str(session_id)}
        if pending_confirmation:
            metadata_out["pending_confirmation"] = pending_confirmation
        return {
            "id": completion_id,
            "object": "chat.completion",
            "created": created,
            "model": "echodesk",
            "metadata": metadata_out,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": final_answer},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"completion_tokens": count_tokens(final_answer)},
        }

    # ---- 流式（SSE）：bridge 事件 → OpenAI chunk ----
    async def event_stream() -> AsyncIterator[str]:
        """把图事件转成 SSE 分片（首片带 session_id，finish 片带 usage/citations）。"""
        answer_parts: list[str] = []
        first_chunk = True
        # chat.turn 根 span 覆盖装配/生成/工具循环/落库全程
        with tracing.turn(
            session_id=str(session_id),
            user_id=str(user.id),
            tags=["chat"],
            workspace_id=str(ws_id),
            stream=True,
            model=get_settings().llm_model,
            query=tracing.snippet(query),
        ):
            pending_confirmation = None
            async for kind, data in bridge.stream(state):
                if kind == "delta":
                    answer_parts.append(str(data))
                    chunk = _chunk(completion_id, created, {"content": str(data)})
                    if first_chunk:
                        chunk["metadata"] = {"session_id": str(session_id)}
                        first_chunk = False
                    yield _sse(chunk)
                elif kind == "tool_call":
                    pending_confirmation = data
                    yield _sse(
                        _chunk(
                            completion_id,
                            created,
                            {},
                            metadata={
                                "session_id": str(session_id),
                                "tool_call": data,
                            },
                        )
                    )
                elif kind == "error":
                    message = str(data["message"])
                    is_llm = data.get("kind") == "LLMError"
                    error_type = "upstream_error" if is_llm else "internal_error"
                    yield _sse({"error": {"message": message, "type": error_type}})
                    yield "data: [DONE]\n\n"
                    return
                else:  # done
                    pending_confirmation = data.get("interrupt") or pending_confirmation
            finish_metadata: dict = {"session_id": str(session_id)}
            if pending_confirmation:
                finish_metadata["pending_confirmation"] = pending_confirmation
            cited = bridge.citations()
            if cited:
                finish_metadata["citations"] = [c.model_dump() for c in _citations(cited)]
            yield _sse(
                _chunk(
                    completion_id,
                    created,
                    {},
                    metadata=finish_metadata,
                    finish_reason="stop",
                )
                | {
                    "usage": {
                        "completion_tokens": count_tokens("".join(answer_parts)),
                    }
                }
            )
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post(
    "/chat/resume",
    summary="恢复被确认流挂起的对话（external 工具裁决后，SSE 续传）",
    response_model=None,
)
async def chat_resume(payload: ChatResumeRequest, user: CurrentUser, db: DbDep):
    """从 checkpoint 恢复挂起的图：approve 执行工具 / deny 拒绝，续传剩余回答。

    Args:
        payload: 裁决请求（workspace/session/call_id/approve）。
        user: 确认人（审计 approver_id）。
        db: 数据库会话。

    Returns:
        StreamingResponse（SSE，同 /chat/completions 流式事件）。

    Raises:
        AppError: workspace 非法（422）/ 非成员（403）/ 会话不存在（404）/
            无可恢复的挂起对话（409）。
    """
    ws_id = await require_ws_member(payload.workspace_id, user, db)
    session_id = uuid_mod.UUID(payload.session_id)
    session = await session_repo.get_by_id(db, workspace_id=ws_id, session_id=session_id)
    if session is None:
        raise NotFoundError("会话不存在")
    # 预检裁决对象（流开始后无法再改 HTTP 状态码）：必须是本 workspace 的
    # pending 调用——终态已裁决 / 记录不存在 / thread 丢失都在此拦下
    call_uuid = uuid_mod.UUID(payload.call_id)
    log = await tool_call_repo.get_log(db, ws_id=ws_id, call_id=call_uuid)
    if log is None:
        raise NotFoundError("确认记录不存在")
    if log.status != ToolCallStatus.PENDING:  # StrEnum：DB 回读为 str，必须 ==
        raise AppError("tool_call_not_pending", 409, f"该调用已处于 {log.status} 终态")

    try:
        llm = get_llm_client()
    except LLMNotConfigured as exc:
        raise AppError("llm_not_configured", 503, str(exc)) from exc
    service = ChatService(llm, WorkingMemory(get_redis()))

    completion_id = f"chatcmpl-{uuid_mod.uuid4().hex}"
    created = int(time.time())
    bridge = _AgentBridge(
        db,
        ws_id=ws_id,
        user=user,
        session_id=session_id,
        user_msg_id=uuid_mod.uuid4(),  # resume 路径不使用（state 从 checkpoint 取）
        user_content="",
        service=service,
        llm=llm,
        enable_tools=True,
    )
    command = Command(resume={"call_id": payload.call_id, "approve": payload.approve})

    async def resume_stream() -> AsyncIterator[str]:
        """恢复路径的 SSE 分片（与主对话同构）。"""
        answer_parts: list[str] = []
        first_chunk = True
        with tracing.turn(
            session_id=str(session_id),
            user_id=str(user.id),
            tags=["chat", "resume"],
            workspace_id=str(ws_id),
            stream=True,
            model=get_settings().llm_model,
            query=f"resume:{payload.call_id}",
        ):
            try:
                async for kind, data in bridge.stream(command):
                    if kind == "delta":
                        answer_parts.append(str(data))
                        chunk = _chunk(completion_id, created, {"content": str(data)})
                        if first_chunk:
                            chunk["metadata"] = {"session_id": payload.session_id}
                            first_chunk = False
                        yield _sse(chunk)
                    elif kind == "error":
                        message = str(data["message"])
                        lowered = message.lower()
                        if "no pending interrupt" in lowered or "not found" in lowered:
                            raise AppError("no_pending_confirmation", 409, "无可恢复的挂起对话")
                        error_type = (
                            "upstream_error" if data.get("kind") == "LLMError" else "internal_error"
                        )
                        yield _sse({"error": {"message": message, "type": error_type}})
                        yield "data: [DONE]\n\n"
                        return
                yield _sse(
                    _chunk(
                        completion_id,
                        created,
                        {},
                        metadata={"session_id": payload.session_id},
                        finish_reason="stop",
                    )
                    | {"usage": {"completion_tokens": count_tokens("".join(answer_parts))}}
                )
                yield "data: [DONE]\n\n"
            except LLMError as exc:
                logger.error("chat_resume_llm_error session=%s error=%s", session_id, exc)
                yield _sse({"error": {"message": str(exc), "type": "upstream_error"}})
                yield "data: [DONE]\n\n"

    return StreamingResponse(resume_stream(), media_type="text/event-stream")


@router.post(
    "/tasks/completions",
    summary="无状态任务补全（Open WebUI 标题/跟进生成等，不落库）",
)
async def task_completions(payload: TaskCompletionRequest, user: CurrentUser, db: DbDep) -> dict:
    """纯 LLM 透传：不建会话、不落库、不写 Working Memory（避免任务 prompt 污染会话历史）。"""
    await _require_member(payload.metadata, user, db)
    try:
        llm = get_llm_client()
    except LLMNotConfigured as exc:
        raise AppError("llm_not_configured", 503, str(exc)) from exc

    messages = [{"role": m.role, "content": m.content} for m in payload.messages]
    try:
        answer, usage = await llm.complete(messages)
    except LLMError as exc:
        raise AppError("llm_upstream_error", 502, str(exc)) from exc
    return {
        "id": f"taskcmpl-{uuid_mod.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "echodesk",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
        },
    }
