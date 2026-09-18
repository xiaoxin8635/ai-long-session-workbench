"""Chat 端点：POST /v1/chat/completions（OpenAI 兼容，docs/01 §6.1 / docs/06 §M-08）。

协议要点：
  - 请求带 EchoDesk 扩展 metadata（workspace_id 必填 / session_id 可选自动建会话）
  - Bearer JWT 认证 + workspace 成员校验（与 /api/* 同一套防线）
  - 流式：SSE 分片 delta → finish 分片（含 usage 估算）→ [DONE]
  - 非流式：标准 chat.completion JSON
  - LLM 未配置 → 503；上游失败 → 502（流开始后以 error 事件收尾）
  - /v1/tasks/completions：Open WebUI 标题/跟进等无状态任务，不落库纯透传
"""

import json
import logging
import time
import uuid as uuid_mod
from collections.abc import AsyncIterator

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.context.tokenizer import count_tokens
from app.core.deps import CurrentUser, DbDep, get_redis, require_ws_member
from app.core.errors import AppError
from app.llm.client import LLMError, LLMNotConfigured, get_llm_client
from app.memory.working_memory import WorkingMemory
from app.schemas.chat import ChatCompletionRequest, ChatMetadata, TaskCompletionRequest
from app.services.chat_service import ChatService

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


@router.post(
    "/chat/completions",
    summary="OpenAI 兼容对话补全（EchoDesk 扩展 metadata）",
    response_model=None,  # 流式返回 StreamingResponse / 非流式返回裸 JSON
)
async def chat_completions(payload: ChatCompletionRequest, user: CurrentUser, db: DbDep):
    """M1 直通版：会话持久化 + Working Memory 窗口 + LLM 透传。"""
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

    # ---- 非流式 ----
    if not payload.stream:
        try:
            answer = await service.complete_answer(
                db,
                ws_id=ws_id,
                user_id=user.id,
                session_id=session_id,
                payload=payload,
                user_msg_id=user_msg_id,
            )
        except LLMError as exc:
            raise AppError("llm_upstream_error", 502, str(exc)) from exc
        return _non_stream_response(completion_id, created, answer, session_id)

    # ---- 流式（SSE）----
    async def event_stream() -> AsyncIterator[str]:
        answer_parts: list[str] = []
        first_chunk = True
        try:
            async for delta_text in service.stream_answer(
                db,
                ws_id=ws_id,
                user_id=user.id,
                session_id=session_id,
                payload=payload,
                user_msg_id=user_msg_id,
            ):
                answer_parts.append(delta_text)
                chunk: dict = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "echodesk",
                    "choices": [{"index": 0, "delta": {"content": delta_text}}],
                }
                # 首片携带 session_id，客户端凭它延续多轮会话
                if first_chunk:
                    chunk["metadata"] = {"session_id": str(session_id)}
                    first_chunk = False
                yield _sse(chunk)
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "echodesk",
                    "metadata": {"session_id": str(session_id)},
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "completion_tokens": count_tokens("".join(answer_parts)),
                    },
                }
            )
            yield "data: [DONE]\n\n"
        except LLMError as exc:
            logger.error("chat_stream_llm_error session=%s error=%s", session_id, exc)
            yield _sse({"error": {"message": str(exc), "type": "upstream_error"}})
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


def _non_stream_response(completion_id: str, created: int, answer: str, session_id: str) -> dict:
    """构造非流式 OpenAI 响应体（metadata.session_id 供客户端延续会话）。"""
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": "echodesk",
        "metadata": {"session_id": str(session_id)},
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {"completion_tokens": count_tokens(answer)},
    }


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
