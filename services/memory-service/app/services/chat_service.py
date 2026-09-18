"""Chat 服务：M1 直通编排（docs/06 §M-08）。

流程：解析/创建会话 → 持久化用户消息 → Working Memory 取窗口 →
LLM 流式生成 → 持久化回答 + 回写窗口 → SSE 透传。

M3 阶段此编排将升级为 LangGraph 图（retrieve→assemble→generate→tools），
当前保持直通以保证 Open WebUI 先跑通完整对话。
"""
import logging
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.llm.client import LLMClient
from app.memory.working_memory import WorkingMemory
from app.models.user import User
from app.repositories import session_repo
from app.schemas.chat import ChatCompletionRequest
from app.schemas.session import MessageCreate
from app.services.session_service import SessionService

logger = logging.getLogger(__name__)

# M1 直通阶段的窗口预算（M-05 Context Builder 接管后按 profile 配置）
_WINDOW_TOKENS = 4000
# 会话默认标题：取首条用户消息截断
_TITLE_MAX = 30

_session_service = SessionService()


class ChatService:
    """chat/completions 用例编排（流式与非流式共用取上下文与落库逻辑）。"""

    def __init__(self, llm: LLMClient, working_memory: WorkingMemory) -> None:
        """注入 LLM 客户端与 Working Memory（测试可替换为 fake）。"""
        self._llm = llm
        self._wm = working_memory

    async def resolve_session(
        self, db: AsyncSession, *, ws_id: uuid.UUID, user: User, payload: ChatCompletionRequest
    ) -> uuid.UUID:
        """取请求指向的会话；无 session_id 时自动创建（标题取首条用户消息）。"""
        if payload.metadata.session_id:
            session = await session_repo.get_by_id(
                db,
                workspace_id=ws_id,
                session_id=uuid.UUID(payload.metadata.session_id),
            )
            if session is None:
                raise NotFoundError("会话不存在")
            return session.id
        first_user = next(
            (m.content for m in payload.messages if m.role == "user"), "新会话"
        )
        created = await _session_service.create(
            db, ws_id=ws_id, user=user, title=first_user[:_TITLE_MAX]
        )
        return created.id

    async def prepare(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        payload: ChatCompletionRequest,
    ) -> None:
        """持久化本轮用户消息并回写 Working Memory（生成前执行）。"""
        user_msg = payload.messages[-1]
        await _session_service.append_message(
            db,
            ws_id=ws_id,
            session_id=session_id,
            payload=MessageCreate(role=user_msg.role, content=user_msg.content),
        )
        await self._wm.append(session_id, role=user_msg.role, content=user_msg.content)

    async def context_messages(self, session_id: uuid.UUID) -> list[dict[str, str]]:
        """组装 LLM 输入：Working Memory 窗口（role/content）。"""
        entries = await self._wm.window(session_id, max_tokens=_WINDOW_TOKENS)
        return [{"role": e["role"], "content": e["content"]} for e in entries]

    async def finalize(
        self, db: AsyncSession, *, ws_id: uuid.UUID, session_id: uuid.UUID, answer: str
    ) -> None:
        """持久化回答并回写 Working Memory（生成完成后执行）。"""
        await _session_service.append_message(
            db,
            ws_id=ws_id,
            session_id=session_id,
            payload=MessageCreate(role="assistant", content=answer),
        )
        await self._wm.append(session_id, role="assistant", content=answer)

    async def stream_answer(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        payload: ChatCompletionRequest,
    ) -> AsyncIterator[str]:
        """流式生成并逐段产出文本；结束后统一落库。"""
        messages = await self.context_messages(session_id)
        chunks: list[str] = []
        async for delta in self._llm.stream_chat(messages):
            chunks.append(delta)
            yield delta
        await self.finalize(db, ws_id=ws_id, session_id=session_id, answer="".join(chunks))

    async def complete_answer(
        self,
        db: AsyncSession,
        *,
        ws_id: uuid.UUID,
        session_id: uuid.UUID,
        payload: ChatCompletionRequest,
    ) -> str:
        """非流式生成；返回完整回答（落库在内部完成）。"""
        messages = await self.context_messages(session_id)
        answer, _usage = await self._llm.complete(messages)
        await self.finalize(db, ws_id=ws_id, session_id=session_id, answer=answer)
        return answer
